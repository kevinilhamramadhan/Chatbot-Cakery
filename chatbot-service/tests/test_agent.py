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


async def test_agent_replaces_hallucinated_price_with_real_menu(monkeypatch):
    """Live regression: the model sometimes answers menu questions itself with
    invented products and prices instead of calling get_menu. Any money the
    model typed without a tool is fabricated — serve the real catalogue."""
    from app.backend_client import products as products_api

    async def fake_list(only_active=True, kategori=None):
        return FAKE_PRODUCTS
    monkeypatch.setattr(products_api, "list_products", fake_list)

    _mock_retrieval(monkeypatch, 0.0)
    _mock_llm(monkeypatch, AIMessage(
        content="Berikut menu Toti Cakery:\n• Cupcakes isi 9 Vanilla — Rp120.000"))

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "menu apa aja yang ada?", history=[])
    assert "Cupcakes isi 9 Vanilla" not in out      # fabricated item dropped
    assert "Brownies Coklat" in out and "Rp50.000" in out   # real catalogue served


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
