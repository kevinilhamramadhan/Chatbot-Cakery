"""LLM agent: RAG retrieval + scope guard + tool calling.

Design note: tool outputs are returned to the user verbatim instead of being fed
back to the LLM for a second pass. With a small model (qwen3:1.7b) this keeps
real data (prices, order summaries) accurate and avoids hallucinated rephrasing.
"""

import asyncio
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.llm.client import get_llm
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER
from app.rag.store import retrieve
from app.tools.registry import ALL_TOOLS, TOOLS_BY_NAME

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# "Rp50.000", "Rp 50000", "50.000 rupiah" — any money the model typed itself.
_PRICE_RE = re.compile(r"(rp\s?\d|\d[\d.,]*\s*(rupiah|ribu\b))", re.IGNORECASE)

OUT_OF_SCOPE_REPLY = (
    f"Maaf, aku hanya bisa membantu seputar {settings.store_name} ya — menu, pemesanan, "
    "pembayaran, pengiriman, dan info toko. Ada yang bisa kubantu soal itu? 😊"
)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


def _history_view(content: str) -> str:
    """Compact view of a past bot reply for the LLM's context window.

    Raw tool outputs (menu list, product detail) must NOT re-enter the context:
    the small model copies them verbatim as its next answer instead of calling
    the tool — no photo gets queued and prices go stale. A short marker keeps
    the conversational thread while forcing a fresh tool call to show data again.
    """
    if content.startswith("Berikut menu"):
        return "[Aku sudah menampilkan daftar menu via tool get_menu]"
    if content.startswith("Permintaanmu sudah aku teruskan ke admin"):
        # Left verbatim, this reply is the single strongest example in the
        # window and the model copies it: measured, an ordinary "aku mau bento
        # cookies 2" flipped from add_to_cart 3/3 to escalate_to_admin 3/3 once
        # this sentence was in the history.
        return "[Aku sudah meneruskan permintaan itu ke admin via tool escalate_to_admin]"
    if content.startswith("*") and "Harga:" in content:
        produk = content.split("*")[1] if content.count("*") >= 2 else "produk"
        return f"[Aku sudah menampilkan detail {produk} + fotonya via tool get_product_detail]"
    if len(content) > 200:
        return content[:200] + " …(dipotong)"
    return content


async def run_agent(wa_number: str, user_text: str, history: list[dict]) -> str:
    # 1) Retrieval + scope guard (PROMPT §7). retrieve() does blocking I/O
    # (Ollama embed + Chroma query) — keep it off the event loop.
    retrieval = await asyncio.to_thread(retrieve, user_text)
    rag_context = retrieval.context_text() if retrieval.in_scope else None
    logger.info(
        "RAG best_sim=%.3f in_scope=%s", retrieval.best_similarity, retrieval.in_scope
    )

    # LATENCY, not cosmetics: Ollama reuses its KV cache only for the longest
    # COMMON PREFIX of the prompt, and the system block (with the 9 tool
    # definitions right behind it) is that prefix. The FAQ context used to be
    # concatenated into this block, so it changed on every turn and invalidated
    # the whole prefix — each turn re-prefilled ~2.5k tokens on CPU. Measured on
    # this host: variable system block = 39-46s/turn, constant = 3-6s/turn.
    # So the system block stays byte-identical and the retrieved context rides
    # along with the question instead.
    messages: list = [SystemMessage(content=SYSTEM_PROMPT)]
    for h in history:
        messages.append(
            HumanMessage(content=h["content"])
            if h["role"] == "user"
            else AIMessage(content=_history_view(h["content"]))
        )
    # Routing reminder (see TOOL_REMINDER note in prompt.py: Ollama collates it
    # into the top system block — the dataset/eval reproduce that placement).
    messages.append(SystemMessage(content=TOOL_REMINDER))
    question = user_text
    if rag_context:
        question = (
            "KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\n"
            + rag_context
            + "\n\nPertanyaan pelanggan: "
            + user_text
        )
    messages.append(HumanMessage(content=question))

    llm = get_llm().bind_tools(ALL_TOOLS)

    try:
        ai: AIMessage = await llm.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM invocation failed: %s", exc)
        return "Maaf, lagi ada gangguan di sistem kami. Coba beberapa saat lagi ya 🙏"

    # 2) No tool call -> direct answer (FAQ / greeting / refusal).
    if not getattr(ai, "tool_calls", None):
        answer = _clean(ai.content)
        # A price the model typed itself is a made-up price. Observed live, even
        # on v4: "menu apa aja yang ada?" sometimes skips get_menu and answers
        # "• Cupcakes isi 9 Vanilla — Rp120.000" — products and prices that do
        # not exist. Spec rule: never invent harga/stok. So if a reply quotes
        # money without a tool having produced it, throw it away and serve the
        # real catalogue instead.
        if answer and _PRICE_RE.search(answer):
            # A price the model typed itself is invented, and the spec forbids
            # inventing harga/stok — so the sentence is dropped. What replaces it
            # is deliberately NOT chosen by classifying the question: guessing
            # "this looks like a menu question" answered "udah aku bayar kok"
            # with the entire price list. Say nothing we cannot ground, and let
            # the customer's next message route normally through the model.
            logger.warning("Ungrounded price in a tool-less reply — dropping it")
            return (
                "Biar aku nggak salah sebut angka, harga selalu kuambil dari sistem ya. "
                "Boleh sebutkan kuenya, atau ketik *menu* untuk daftar lengkapnya 😊"
            )
        # Hard scope guard: out-of-scope and the model didn't use any on-topic
        # tool -> refuse rather than answer from general knowledge.
        if not rag_context and not answer:
            return OUT_OF_SCOPE_REPLY
        return answer or OUT_OF_SCOPE_REPLY

    # 3) Execute tools; their outputs are the user-facing reply.
    outputs: list[str] = []
    for tc in ai.tool_calls:
        tool = TOOLS_BY_NAME.get(tc["name"])
        if tool is None:
            logger.warning("LLM requested unknown tool: %s", tc["name"])
            continue
        try:
            result = await tool.ainvoke(tc["args"])
            outputs.append(str(result))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed: %s", tc["name"], exc)
            outputs.append("Maaf, ada kendala saat memproses permintaanmu. Coba lagi ya 🙏")

    if not outputs:
        return _clean(ai.content) or OUT_OF_SCOPE_REPLY
    return "\n\n".join(outputs)
