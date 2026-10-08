"""Agent wiring tests: tool-call execution + scope guard (LLM mocked, no Ollama)."""

import pytest
from langchain_core.messages import AIMessage

from app.conversation.context import TurnContext, set_turn_context
from app.llm import agent as agent_mod
from app.rag.store import RetrievalResult

WA = "628123456789@c.us"

FAKE_PRODUCTS = [
    {"id": 5, "nama_produk": "Brownies Coklat", "harga_jual": 50000, "is_active": True},
]


class _FakeBound:
    def __init__(self, ai):
        self._ai = ai

    async def ainvoke(self, messages):
        return self._ai


class _FakeLLM:
    def __init__(self, ai):
        self._ai = ai

    def bind_tools(self, tools):
        return _FakeBound(self._ai)


def _mock_llm(monkeypatch, ai_message):
    monkeypatch.setattr(agent_mod, "get_llm", lambda: _FakeLLM(ai_message))


def _mock_retrieval(monkeypatch, similarity):
    def fake_retrieve(query, top_k=None):
        return RetrievalResult(
            documents=["Q: jam buka? A: 09-19"] if similarity > 0 else [],
            metadatas=[{}],
            similarities=[similarity] if similarity > 0 else [],
        )
    monkeypatch.setattr(agent_mod, "retrieve", fake_retrieve)


async def test_agent_executes_tool_call(monkeypatch):
    from app.backend_client import products as products_api

    async def fake_list(only_active=True, kategori=None):
        return FAKE_PRODUCTS
    monkeypatch.setattr(products_api, "list_products", fake_list)

    _mock_retrieval(monkeypatch, 0.0)
    ai = AIMessage(content="", tool_calls=[
        {"name": "get_menu", "args": {}, "id": "1", "type": "tool_call"}
    ])
    _mock_llm(monkeypatch, ai)

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "menu apa aja", history=[])
    assert "Brownies Coklat" in out          # real tool output reached the user
    assert "Rp50.000" in out


async def test_agent_answers_from_faq_when_in_scope(monkeypatch):
    _mock_retrieval(monkeypatch, 0.9)        # high similarity -> in scope
    ai = AIMessage(content="Kami buka jam 09.00-19.00 WIB.")
    _mock_llm(monkeypatch, ai)

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "jam buka?", history=[])
    assert "09.00" in out


async def test_agent_scope_guard_refuses_out_of_topic(monkeypatch):
    _mock_retrieval(monkeypatch, 0.05)       # below threshold -> out of scope
    ai = AIMessage(content="")               # model produced nothing usable
    _mock_llm(monkeypatch, ai)

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "siapa presiden?", history=[])
    assert out == agent_mod.OUT_OF_SCOPE_REPLY


async def test_agent_drops_a_hallucinated_price(monkeypatch):
    """Live regression: the model sometimes answers menu questions itself with
    invented products and prices instead of calling get_menu. Any money the
    model typed without a tool is fabricated, so the sentence is dropped.

    What replaces it is a fixed, groundable line — NOT a guess at what the
    customer wanted. Classifying the question here ("looks like a menu ask ->
    serve the menu") answered "udah aku bayar kok" with the whole price list."""
    _mock_retrieval(monkeypatch, 0.0)
    _mock_llm(monkeypatch, AIMessage(
        content="Berikut menu Toti Cakery:\n• Cupcakes isi 9 Vanilla — Rp120.000"))

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "menu apa aja yang ada?", history=[])
    assert "Cupcakes isi 9 Vanilla" not in out      # fabricated item dropped
    assert "Rp120.000" not in out                   # fabricated price dropped
    assert "menu" in out.lower()                    # points at the real route


async def test_agent_leaves_price_free_answers_alone(monkeypatch):
    _mock_retrieval(monkeypatch, 0.9)
    _mock_llm(monkeypatch, AIMessage(content="Kami buka jam 09.00-19.00 WIB."))
    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "jam buka?", history=[])
    assert out == "Kami buka jam 09.00-19.00 WIB."


async def test_agent_strips_think_tags(monkeypatch):
    _mock_retrieval(monkeypatch, 0.0)
    ai = AIMessage(content="<think>reasoning here</think>Halo! Ada yang bisa dibantu?")
    _mock_llm(monkeypatch, ai)

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "halo", history=[])
    assert "reasoning here" not in out
    assert "Halo!" in out


async def test_tool_owner_tidak_dijalankan_untuk_pelanggan(monkeypatch):
    """Lapis kedua: definisinya memang tidak dimuat, tapi nama tool bisa saja
    muncul dari ingatan hasil fine-tuning. Panggilannya tetap harus mental."""
    from app.backend_client import api as backend
    from app.conversation import rbac
    from app.tools import registry

    async def direktori():
        return [{"nomor_wa": "6285702286413", "role": "Owner", "level": 1,
                 "handles_takeover": True}]

    monkeypatch.setattr(backend, "get_role_directory", direktori)
    rbac.bersihkan_cache()

    dimuat = []

    class _Perekam(_FakeLLM):
        def bind_tools(self, tools):
            dimuat.append({t.name for t in tools})
            return _FakeBound(self._ai)

    ai = AIMessage(content="", tool_calls=[
        {"name": "financial_report", "args": {}, "id": "1", "type": "tool_call"}
    ])
    monkeypatch.setattr(agent_mod, "get_llm", lambda: _Perekam(ai))
    _mock_retrieval(monkeypatch, 0.0)

    set_turn_context(TurnContext(wa_number=WA))
    jawab = await agent_mod.run_agent(WA, "laporan keuangan dong", [])

    assert dimuat and not (dimuat[0] & registry.NAMA_TOOL_OWNER), dimuat
    assert "Omzet" not in jawab and "Laporan Keuangan" not in jawab, jawab



async def test_pesan_satu_kata_tidak_membawa_konteks_faq(monkeypatch):
    """ "yes" lolos ambang kemiripan dan dijawab dengan isi FAQ. Pesan satu kata
    tanpa tanda tanya tidak diberi konteks, jadi tidak ada yang bisa disalin."""
    terlihat = []

    class _LLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            terlihat.append(messages[-1].content)
            return AIMessage(content="Siap kak 😊")

    monkeypatch.setattr(agent_mod, "get_llm", lambda: _LLM())
    _mock_retrieval(monkeypatch, 0.9)
    await agent_mod.run_agent("628111", "yes", [])
    await agent_mod.run_agent("628111", "halal?", [])
    assert "KONTEKS FAQ" not in terlihat[0] and "KONTEKS FAQ" in terlihat[1]


async def test_faq_yang_menyebut_admin_ikut_menawarkan_sambungan(monkeypatch):
    """Terukur 8 Okt 2026: "mau pesen kue custom" dijawab FAQ "diteruskan ke admin
    kami" tanpa tawaran apa pun, jadi "ya" pelanggan tidak menyambungkan siapa-siapa."""
    from app.conversation import escalation, store

    def fake_retrieve(query, top_k=None):
        faq = {"pertanyaan": "jam buka?" if "jam" in query else "kue custom?",
               "jawaban": "09.00-19.00" if "jam" in query else "Kue custom ditangani admin kami."}
        return RetrievalResult(documents=["dok"], metadatas=[faq], similarities=[0.9])
    monkeypatch.setattr(agent_mod, "retrieve", fake_retrieve)
    _mock_llm(monkeypatch, AIMessage(content="belum punya infonya"))
    dicatat = []

    async def fake_set(wa, reason):
        dicatat.append(reason)
    monkeypatch.setattr(store, "set_pending_escalation", fake_set)

    out = await agent_mod.run_agent(WA, "mau pesen kue custom dong", history=[])
    assert out.startswith("Kue custom ditangani admin kami.") and escalation.OFFER_TEXT in out
    assert dicatat == ["mau pesen kue custom dong"]
    assert await agent_mod.run_agent(WA, "jam buka?", history=[]) == "09.00-19.00"
    assert len(dicatat) == 1
