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
from app.llm.prompt import SYSTEM_PROMPT
from app.rag.store import retrieve
from app.tools.registry import ALL_TOOLS, TOOLS_BY_NAME

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

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

    system = SYSTEM_PROMPT
    if rag_context:
        system += (
            "\n\nKONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\n" + rag_context
        )

    messages: list = [SystemMessage(content=system)]
    for h in history:
        messages.append(
            HumanMessage(content=h["content"])
            if h["role"] == "user"
            else AIMessage(content=_history_view(h["content"]))
        )
    # Routing reminder NEXT TO the question: small models weigh the nearest
    # instruction far more than rules buried at the top of a long system prompt.
    messages.append(SystemMessage(content=(
        "INGAT ATURAN TOOL: jika pesan berikut menyebut nama SATU produk dan "
        "menanyakan produk itu (kayak gimana/seperti apa/foto/detail), panggil "
        "get_product_detail dengan nama produk itu. get_menu HANYA untuk minta "
        "daftar semua menu. Jangan meniru pola jawaban sebelumnya."
    )))
    messages.append(HumanMessage(content=user_text))

    llm = get_llm().bind_tools(ALL_TOOLS)

    try:
        ai: AIMessage = await llm.ainvoke(messages)
    except Exception as exc:  # noqa: BLE001
        logger.exception("LLM invocation failed: %s", exc)
        return "Maaf, lagi ada gangguan di sistem kami. Coba beberapa saat lagi ya 🙏"

    # 2) No tool call -> direct answer (FAQ / greeting / refusal).
    if not getattr(ai, "tool_calls", None):
        answer = _clean(ai.content)
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
