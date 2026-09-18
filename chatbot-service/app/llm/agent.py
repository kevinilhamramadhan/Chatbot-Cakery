"""LLM agent: RAG retrieval + scope guard + tool calling.

Design note: tool outputs are returned to the user verbatim instead of being fed
back to the LLM for a second pass. With a small model (qwen3:1.7b) this keeps
real data (prices, order summaries) accurate and avoids hallucinated rephrasing.
"""

import asyncio
import logging
import re
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.conversation import bahasa
from app.conversation.context import get_turn_context_or_none
from app.core.config import settings
from app.llm.client import get_llm
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER
from app.rag.store import retrieve
from app.tools.registry import NAMA_TOOL_OWNER, TOOLS_BY_NAME, tools_untuk

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# "Rp50.000", "Rp 50000", "50.000 rupiah" — any money the model typed itself.
_PRICE_RE = re.compile(r"(rp\s?\d|\d[\d.,]*\s*(rupiah|ribu\b))", re.IGNORECASE)
# A report the model typed itself is as invented as a price it typed itself.
# Observed live: an Owner asking "produk apa yang paling laku bulan ini?" got a
# whole "📈 *Analitik Bisnis*" block with no tool call behind it.
_REPORT_RE = re.compile(r"(📊|📈|laporan keuangan|analitik bisnis)", re.IGNORECASE)
_TOOLNAME_RE = re.compile(
    r"\b(get_menu|get_product_detail|add_to_cart|compare_products|get_order_status|"
    r"check_payment_status|cancel_order|escalate_to_admin|financial_report|"
    r"business_analytics|check_cart|resend_payment_method|send_apology)\b",
    re.IGNORECASE,
)

# Konstanta versi Indonesianya dipertahankan untuk uji; jalur sungguhan memakai
# _di_luar_cakupan() supaya ikut bahasa pelanggan.
OUT_OF_SCOPE_REPLY = bahasa.teks("di_luar_cakupan", bahasa.ID, toko=settings.store_name)


def _di_luar_cakupan(lang: str) -> str:
    return bahasa.teks("di_luar_cakupan", lang, toko=settings.store_name)


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _THINK_RE.sub("", text).strip()


# Awalan penanda keluaran tool, dihitung sekali dari templat supaya tidak
# pernah menyimpang dari teks yang benar-benar dikirim.
_AWALAN_MENU = tuple(
    bahasa.teks("menu_judul", l, toko=settings.store_name).split("{")[0][:12]
    for l in (bahasa.ID, bahasa.EN)
)
_AWALAN_TAKEOVER = tuple(
    bahasa.teks("diteruskan_ke_admin", l)[:24] for l in (bahasa.ID, bahasa.EN)
)


def pertanyaan_dengan_konteks(user_text: str, rag_context: str | None) -> str:
    """Pesan pelanggan seperti yang diterima model: FAQ hasil RAG menumpang di depannya.

    Satu-satunya tempat bentuk ini ditulis — dataset fine-tuning
    (finetune/generate_dataset.py) memanggil fungsi yang sama, jadi yang dilatih
    dan yang dilayani tidak bisa bergeser diam-diam.
    """
    if not rag_context:
        return user_text
    return ("KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\n"
            + rag_context + "\n\nPertanyaan pelanggan: " + user_text)


def _history_view(content: str) -> str:
    """Compact view of a past bot reply for the LLM's context window.

    Raw tool outputs (menu list, product detail) must NOT re-enter the context:
    the small model copies them verbatim as its next answer instead of calling
    the tool — no photo gets queued and prices go stale. A short marker keeps
    the conversational thread while forcing a fresh tool call to show data again.
    """
    # Kedua bahasa dicocokkan: kalau hanya awalan Indonesia yang dicari, menu
    # berbahasa Inggris tidak pernah terkompres dan masuk utuh ke konteks —
    # persis keadaan yang fungsi ini dibuat untuk mencegah.
    if content.startswith(_AWALAN_MENU):
        return "[Aku sudah menampilkan daftar menu via tool get_menu]"
    if content.startswith(_AWALAN_TAKEOVER):
        # Left verbatim, this reply is the single strongest example in the
        # window and the model copies it: measured, an ordinary "aku mau bento
        # cookies 2" flipped from add_to_cart 3/3 to escalate_to_admin 3/3 once
        # this sentence was in the history.
        return "[Aku sudah meneruskan permintaan itu ke admin via tool escalate_to_admin]"
    if content.startswith("*") and "Harga:" in content:
        produk = content.split("*")[1] if content.count("*") >= 2 else "produk"
        return (
            f"[Aku sudah menampilkan detail {produk} + fotonya via tool "
            f"get_product_detail, dan menanyakan mau pesan berapa banyak]"
        )
    if len(content) > 200:
        return content[:200] + " …(dipotong)"
    return content


async def run_agent(wa_number: str, user_text: str, history: list[dict]) -> str:
    # 1) Retrieval + scope guard (PROMPT §7). retrieve() does blocking I/O
    # (Ollama embed + Chroma query) — keep it off the event loop.
    _ctx = get_turn_context_or_none()
    mulai = _ctx.mulai if _ctx is not None else time.monotonic()
    try:
        retrieval = await asyncio.wait_for(asyncio.to_thread(retrieve, user_text),
                                           timeout=settings.batas_rag_detik)
    except TimeoutError:
        # Lebih baik menjawab tanpa FAQ daripada melewati batas waktu balasan.
        logger.warning("RAG melewati %.0fs — giliran lanjut tanpa konteks FAQ",
                       settings.batas_rag_detik)
        retrieval = None
    rag_context = retrieval.context_text() if retrieval and retrieval.in_scope else None
    if retrieval is not None:
        logger.info(
            "RAG best_sim=%.3f in_scope=%s", retrieval.best_similarity, retrieval.in_scope
        )
        if _ctx is not None:
            _ctx.rag_similarity = retrieval.best_similarity
            _ctx.rag_in_scope = retrieval.in_scope

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
    messages.append(HumanMessage(content=pertanyaan_dengan_konteks(user_text, rag_context)))

    # Tool Owner tidak dimuat untuk pelanggan biasa — bukan cuma ditolak waktu
    # dipanggil. Prefix KV-cache tetap aman: daftarnya konstan per peran, jadi
    # yang ada hanya dua bentuk prompt, bukan berubah tiap giliran.
    from app.conversation import store

    lang = await store.get_lang(wa_number)
    tools = await tools_untuk(wa_number)
    diizinkan = {t.name for t in tools}
    llm = get_llm().bind_tools(tools)

    sisa = settings.batas_balas_detik - (time.monotonic() - mulai)
    try:
        # Membatalkan ainvoke memutus koneksi HTTP, dan Ollama menghentikan
        # generasinya — CPU langsung bebas untuk pesan berikutnya.
        ai: AIMessage = await asyncio.wait_for(llm.ainvoke(messages), timeout=max(1.0, sisa))
    except TimeoutError:
        logger.warning("LLM melewati batas balasan (%.0fs) — kirim balasan tetap",
                       settings.batas_balas_detik)
        return bahasa.teks("balasan_lambat", lang)
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
        # Same rule, other shapes: a report with no tool behind it is invented,
        # and the internal tool names are not something a customer should read.
        if answer and _REPORT_RE.search(answer):
            logger.warning("Ungrounded report in a tool-less reply — dropping it")
            return (
                "Angka laporan selalu kuambil dari sistem, jadi aku nggak bisa "
                "menyebutkannya sendiri. Coba minta lagi ya — nanti kuambilkan "
                "dari data yang sebenarnya 🙏"
            )
        if answer and _TOOLNAME_RE.search(answer):
            logger.warning("Reply mentioned an internal tool name — dropping it")
            return (
                "Boleh diulang maksudnya kak? Aku bisa bantu soal menu, pemesanan, "
                "pembayaran, dan status pesanan 😊"
            )
        # Hard scope guard: out-of-scope and the model didn't use any on-topic
        # tool -> refuse rather than answer from general knowledge.
        if not rag_context and not answer:
            return _di_luar_cakupan(lang)
        return answer or _di_luar_cakupan(lang)

    # 3) Execute tools; their outputs are the user-facing reply.
    outputs: list[str] = []
    for tc in ai.tool_calls:
        if tc["name"] not in diizinkan:
            # Modelnya tidak diberi definisi tool ini, tapi nama tool bisa saja
            # muncul dari ingatan hasil fine-tuning. Dihentikan di sini juga.
            logger.warning("Tool %s diminta oleh nomor yang tidak berhak",
                           tc["name"] if tc["name"] in NAMA_TOOL_OWNER else "tak dikenal")
            continue
        tool = TOOLS_BY_NAME.get(tc["name"])
        if tool is None:
            logger.warning("LLM requested unknown tool: %s", tc["name"])
            continue
        try:
            if _ctx is not None:
                _ctx.tools_called.append(tc["name"])
            result = await tool.ainvoke(tc["args"])
            outputs.append(str(result))
        except Exception as exc:  # noqa: BLE001
            logger.exception("Tool %s failed: %s", tc["name"], exc)
            outputs.append("Maaf, ada kendala saat memproses permintaanmu. Coba lagi ya 🙏")

    if not outputs:
        return _clean(ai.content) or _di_luar_cakupan(lang)
    return "\n\n".join(outputs)
