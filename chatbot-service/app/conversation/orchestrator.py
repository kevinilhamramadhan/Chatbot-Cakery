"""Main conversation brain: routes an inbound WhatsApp message to a reply.

Handles human-takeover suppression, the deterministic order flow (cart confirm ->
identity -> payment-type -> checkout), and delegates open-ended turns to the LLM
agent. Returns a Reply; the caller is responsible for actually sending it.
"""

import logging
import re
import time
from dataclasses import dataclass, field

from app.backend_client import api as backend
from app.conversation import checkout, escalation, store, verification
from app.conversation.context import (
    OutboundMedia,
    TurnContext,
    get_turn_context_or_none,
    set_turn_context,
)
from app.conversation.states import (
    State,
    mentions_quantity,
    text_is_cancel,
    text_is_confirm,
    text_is_gratitude,
)
from app.core.config import settings
from app.core.security import mask_phone
from app.llm.agent import run_agent

logger = logging.getLogger(__name__)


@dataclass
class Reply:
    text: str | None = None
    media: list[OutboundMedia] = field(default_factory=list)
    suppressed: bool = False  # true when human takeover blocks auto-reply


def _wa_digits(wa_number: str) -> str:
    return "".join(c for c in wa_number if c.isdigit())


# ── Shapes the deterministic steps have to recognise in free text ────────────
_QUESTION_STARTERS = (
    "kenapa", "knp", "kok", "gimana", "gmn", "bagaimana", "apakah", "apa itu",
    "apa aja", "berapa", "brp", "bisakah", "emang", "memang", "siapa", "kapan",
    "dimana", "di mana", "buat apa", "untuk apa", "aman ",
)

# Addresses a courier can actually find are built from these words; "rumah"
# is not one of them.
_ADDRESS_HINT_RE = re.compile(
    r"\b(jl|jln|jalan|gg|gang|blok|perum|perumahan|komplek|kompleks|kav|rt|rw|"
    r"no|nomor|desa|dusun|kel|kelurahan|kec|kecamatan|apartemen|apart|tower|"
    r"lantai|ruko|batam|nagoya|sekupang|batam ?centre)\b",
    re.IGNORECASE,
)

# QRIS is checked first: the prompt itself offers GoPay/OVO/Dana as QRIS.
_QRIS_RE = re.compile(
    r"\b(qris|qr|gopay|go-?pay|ovo|dana|shopee ?pay|linkaja|link ?aja|"
    r"e-?wallet|dompet digital|scan)\b",
    re.IGNORECASE,
)
_VA_RE = re.compile(
    r"\b(va|virtual account|transfer|tf|bank|atm|m-?banking|mbanking|"
    r"internet banking|rekening)\b",
    re.IGNORECASE,
)
_FULL_RE = re.compile(r"\b(penuh|full|lunas|sekaligus)\b", re.IGNORECASE)
_DP_RE = re.compile(r"\b(dp|50|separuh|setengah|sebagian|down ?payment)\b", re.IGNORECASE)


def _looks_like_question(text: str) -> bool:
    """True when the customer is asking something instead of answering us.

    The identity steps store whatever is typed, so without this a customer who
    types "kenapa harus kasih nama sih?" is saved to the backend WITH that
    sentence as their name — observed live, and the row is still there.
    """
    t = text.strip().lower()
    return bool(t) and (t.endswith("?") or t.startswith(_QUESTION_STARTERS))


# Cara pelanggan sebenarnya menjawab langkah pengiriman. "anter" ada di daftar
# karena ejaan sehari-harinya begitu: "dianter aja ke rumah" dulu tidak cocok
# dengan "antar" dan pelanggan disuruh mengetik *delivery* padahal jawabannya
# sudah jelas (terukur di suite QA W2).
_KATA_PICKUP = ("pickup", "pick up", "ambil", "mampir", "jemput")
_KATA_DELIVERY = (
    "delivery", "kirim", "antar", "anter", "kurir", "ojol", "gosend", "gojek",
    "grab", "grabexpress", "ke rumah", "ke alamat",
)


# ── Identity validation ───────────────────────────────────────────────────────
# Words that answer a LATER step. Alone they are not names, and accepting them
# produced "Halo dikirim! Sekarang, boleh minta *alamat*-mu?".
_STEP_WORDS = {
    "pickup", "delivery", "dikirim", "kirim", "diantar", "dianter", "antar",
    "ambil", "qris", "va", "transfer", "gopay", "ovo", "dana", "dp", "penuh",
    "lunas", "ya", "iya", "oke", "ok", "sudah", "udah", "belum", "halo", "hai",
}


def _valid_name(s: str) -> bool:
    """A plausible person's name, not a whole sentence and not a flow keyword.

    The caps matter because whatever passes here is written to the backend as
    the customer's name: a live run stored "eh tambahin 1 brownies fudgy almond
    dong" as somebody's name before the caller learned to spot a cart change.
    """
    t = s.strip()
    if not (2 <= len(t) <= 60) or t.isdigit() or len(t.split()) > 5:
        return False
    return t.casefold() not in _STEP_WORDS


def _extract_name(text: str) -> str | None:
    """Find the name inside a sentence, instead of rejecting the whole message.

    Customers answer this step in sentences — "namaku Rina Kartika", "kan udah
    aku sebut di atas, Kevin". Rejecting those verbatim left the conversation
    repeating "Namanya sepertinya kurang tepat" with no way forward.
    """
    t = text.strip()
    # "namaku Rina Kartika" is a valid-looking name on its own, so the prefix has
    # to be stripped BEFORE the plain check — otherwise the customer is greeted
    # "Halo namaku Rina Kartika!" and the backend stores it that way.
    m = re.search(r"\bnama(?:ku|nya|nya adalah)?\s*:?\s+(.{2,60})$", t, re.IGNORECASE)
    if m and _valid_name(m.group(1)):
        return m.group(1).strip()
    if _valid_name(t):
        return t
    if "," in t:
        tail = t.rsplit(",", 1)[1].strip()
        if _valid_name(tail):
            return tail
    return None


def _valid_address(s: str) -> bool:
    """Length alone let "rumah" through and a real order was created for it."""
    t = s.strip()
    if len(t) < 10:
        return False
    return bool(re.search(r"\d", t) or _ADDRESS_HINT_RE.search(t))


async def handle_message(wa_number: str, text: str) -> Reply:
    text = (text or "").strip()

    # 0) Verifikasi nomor untuk pendaftaran Buyer Site ("VERIFIKASI <kode>").
    # Ditaruh paling atas, sebelum takeover: pelanggan sedang menunggu di halaman
    # pendaftaran, jadi verifikasi tidak boleh ikut dibungkam hanya karena admin
    # kebetulan sedang menangani chat ini. Balasannya deterministik, tidak lewat
    # LLM — status verifikasi bukan hal yang boleh diimprovisasi model.
    code = verification.extract_code(text)
    if code:
        await store.log_message(wa_number, "in", text, intent="wa_verification")
        reply_text = await verification.handle(wa_number, code)
        await store.log_message(wa_number, "out", reply_text)
        return Reply(text=reply_text)

    # 1) Human takeover: log inbound, do NOT auto-reply (PROMPT §12).
    if await store.is_takeover_active(wa_number):
        # Backend is the source of truth — Admin may have ended the takeover
        # from the Admin Site, which the local cache can't see. Only checked
        # while the local flag is on, so the common path stays backend-free.
        if await _takeover_still_active(wa_number):
            await store.log_message(wa_number, "in", text, intent="takeover_suppressed")
            logger.info("Takeover active for %s — suppressing auto-reply", mask_phone(wa_number))
            return Reply(suppressed=True)
        await store.deactivate_takeover(wa_number)

    session = await store.get_or_create_session(wa_number)
    await store.log_message(wa_number, "in", text)
    state = session.state

    # 2) An escalation the model OFFERED earlier. Takeover is a big hammer — it
    # silences the bot for a day — so it only swings on an explicit yes.
    #
    # The offer stands for a few turns rather than one. It used to be cleared by
    # the very next message, which broke the commonest shape of the exchange:
    # the bot offers, the customer asks one more thing, and only then says yes.
    # Widening the window is what let the keyword matcher for "sambungkan ke
    # admin" be deleted — deciding that a message means "connect me" is the
    # model's job, and it already has a tool for it.
    if session.pending_escalation:
        if await _escalation_offer_expired(wa_number):
            # Tawaran yang menggantung terlalu lama berhenti berlaku. Kata "ya"
            # sering muncul di kalimat biasa ("ya udah kirim aja"), dan menerima
            # tawaran dari delapan giliran yang lalu berarti membungkam bot
            # sehari penuh atas sesuatu yang sudah tidak dibicarakan lagi.
            await store.set_pending_escalation(wa_number, None)
        elif (
            text_is_confirm(text)
            and not text_is_cancel(text)
            and not text_is_gratitude(text)
            # Pesan yang membawa angka adalah jawaban tentang pesanan, bukan izin
            # menyambungkan ke admin. Terukur: "dua ya" (jawaban jumlah kue)
            # diterima sebagai persetujuan karena "ya" ada di CONFIRM_WORDS, dan
            # bot langsung bungkam. Aturan yang sama sudah dipakai di langkah
            # konfirmasi keranjang.
            and not mentions_quantity(text)
        ):
            await store.set_pending_escalation(wa_number, None)
            reply = Reply(text=await escalation.start_takeover(
                wa_number, session.pending_escalation))
            await store.log_message(wa_number, "out", reply.text)
            return reply
        elif text_is_cancel(text):
            await store.set_pending_escalation(wa_number, None)

    # One context per inbound message, set here rather than inside the agent
    # branch: the deterministic steps need it too (tools read `user_text`), and
    # a leftover context from the previous message would make the turn log lie.
    set_turn_context(TurnContext(wa_number=wa_number, user_text=text))

    started = time.monotonic()
    if state == State.AWAITING_CART_CONFIRMATION:
        reply = await _handle_confirmation(wa_number, text)
    elif state == State.COLLECTING_IDENTITY:
        reply = await _handle_identity(wa_number, text)
    else:
        # IDLE / AWAITING_PAYMENT / ORDER_ACTIVE -> LLM agent (with tools).
        reply = await _run_agent_turn(wa_number, text)
    _log_turn(wa_number, state, started)

    if reply.text:
        await store.log_message(wa_number, "out", reply.text)
    return reply


_MAKS_GILIRAN_TAWARAN = 3


async def _escalation_offer_expired(wa_number: str) -> bool:
    """Sudah berapa giliran sejak bot menawarkan sambungan ke admin?

    Umurnya dihitung dari riwayat, bukan dari kolom baru: pesan tawarannya
    berbunyi tetap, jadi cukup dicari kapan terakhir dikirim lalu dihitung
    berapa pesan pelanggan yang datang sesudahnya (termasuk yang sekarang).
    """
    riwayat = await store.recent_history(wa_number, limit=12)
    penanda = escalation.OFFER_TEXT[:40]
    terakhir = -1
    for i, pesan in enumerate(riwayat):
        if pesan["role"] == "assistant" and pesan["content"].startswith(penanda):
            terakhir = i
    if terakhir < 0:
        # Tawarannya sudah terlalu jauh ke belakang untuk terlihat di riwayat.
        return True
    sesudahnya = sum(1 for p in riwayat[terakhir + 1:] if p["role"] == "user")
    return sesudahnya > _MAKS_GILIRAN_TAWARAN


def _log_turn(wa_number: str, state: str, started: float) -> None:
    """One line per inbound message, so a conversation can be traced from logs.

    Before this, tool calls were not logged at all: the only way to find out
    which tool a turn used was to re-run the QA harness against the model, which
    is far too expensive for everyday troubleshooting.
    """
    ctx = get_turn_context_or_none()
    tools = ",".join(ctx.tools_called) if ctx and ctx.tools_called else "-"
    sim = f"{ctx.rag_similarity:.3f}" if ctx and ctx.rag_similarity is not None else "-"
    scope = ("in" if ctx.rag_in_scope else "out") if ctx and ctx.rag_in_scope is not None else "-"
    logger.info(
        "turn %s | state=%s | rag=%s/%s | tools=%s | %dms",
        mask_phone(wa_number), state, sim, scope, tools,
        int((time.monotonic() - started) * 1000),
    )


async def _takeover_still_active(wa_number: str) -> bool:
    try:
        st = await backend.get_takeover_status(wa_number)
    except Exception:  # noqa: BLE001 - backend unreachable -> trust local flag
        return True
    if st is None:
        # 404 means the backend has never heard of this number, not that the
        # takeover ended: it only learns about a customer at checkout, while the
        # commonest escalation is a NEW customer asking for a custom cake.
        # Reading 404 as "no takeover" un-muted the bot one message after it
        # promised a human would take over (observed live).
        return True
    return bool(st.get("human_takeover_active")) and not st.get("is_expired")


async def _answer_then_reask(wa_number: str, text: str, reask: str,
                             back_to: State = State.COLLECTING_IDENTITY) -> Reply:
    """Answer an off-script question, then repeat the step we were standing on.

    State is pinned back afterwards: the deterministic steps own the flow, so a
    tool that tried to move the state (add_to_cart) must not win here.
    """
    reply = await _run_agent_turn(wa_number, text)
    await store.set_state(wa_number, back_to)
    body = (reply.text or "").strip()
    return Reply(text=f"{body}\n\n{reask}" if body else reask, media=reply.media)


async def _run_agent_turn(wa_number: str, text: str) -> Reply:
    ctx = get_turn_context_or_none()
    if ctx is None or ctx.wa_number != wa_number:
        ctx = TurnContext(wa_number=wa_number, user_text=text)
        set_turn_context(ctx)
    # limit=7 lalu buang pesan ini sendiri: handle_message sudah mencatatnya ke
    # log sebelum merutekan, jadi tanpa exclude_last_user model menerima pesan
    # yang sama dua kali dan routing-nya ambruk (lihat recent_history).
    history = await store.recent_history(wa_number, limit=7, exclude_last_user=text)

    answer = await run_agent(wa_number, text, history)
    # A tool (add_to_cart) may have requested a state transition.
    if ctx.next_state:
        await store.set_state(wa_number, ctx.next_state)
    return Reply(text=answer, media=ctx.media)


# ── Cart confirmation step (PROMPT §10.4-5) ───────────────────────────────────
async def _handle_confirmation(wa_number: str, text: str) -> Reply:
    if text_is_cancel(text):
        await store.set_cart(wa_number, [])
        await store.set_state(wa_number, State.IDLE)
        return Reply(text="Oke, pesanan dibatalkan ya. Ada lagi yang bisa kubantu? 😊")

    if text_is_confirm(text) and not mentions_quantity(text):
        cust = await store.get_customer(wa_number)
        if cust.get("channel"):
            # Re-confirmation after checkout bounced the cart back (e.g. a price
            # changed) — identity is already complete, don't ask for it again.
            return Reply(text=await checkout.finalize_order(wa_number))
        await store.set_customer(wa_number, {})  # reset identity collection
        await store.set_state(wa_number, State.COLLECTING_IDENTITY)
        return Reply(
            text="Siap! Untuk memproses pesanan, boleh aku minta *nama* kamu dulu?\n"
                 "(ketik *batal* kalau berubah pikiran)"
        )

    # Everything else goes to the model: a question to answer, a change to the
    # order, a new product — all of it is intent, and reading intent is its job.
    # What used to make this dangerous was the model answering "iya udah bener"
    # by calling add_to_cart again and silently doubling the order. That is now
    # blocked inside the tool itself (it will not grow a line the customer did
    # not put a number on), so the routing here does not have to guess.
    reply = await _run_agent_turn(wa_number, text)
    if (await store.get_or_create_session(wa_number)).state == State.AWAITING_CART_CONFIRMATION:
        body = (reply.text or "").strip()
        reask = ("Kalau pesanannya sudah pas, ketik *sudah sesuai* ya — "
                 "atau *batal* kalau berubah pikiran.")
        return Reply(text=f"{body}\n\n{reask}" if body else reask, media=reply.media)
    return reply


# ── Identity + payment-type collection (PROMPT §10.6-8) ───────────────────────
async def _handle_identity(wa_number: str, text: str) -> Reply:
    if text_is_cancel(text):
        await store.set_cart(wa_number, [])
        await store.set_customer(wa_number, {})
        await store.set_state(wa_number, State.IDLE)
        return Reply(text="Oke, pesanan dibatalkan ya. 😊")

    cust = await store.get_customer(wa_number)

    # Step 1: name
    if "nama" not in cust:
        if _looks_like_question(text):
            return await _answer_then_reask(
                wa_number, text, "Balik ke pesanan ya — boleh aku minta *nama* kamu?")
        nama = _extract_name(text)
        if nama is None:
            # A sentence that holds no name is usually a request, not a bad
            # answer. Repeating "Namanya sepertinya kurang tepat" at it left the
            # conversation in a loop with no way forward.
            if len(text.split()) > 3:
                return await _answer_then_reask(
                    wa_number, text, "Balik ke pesanan ya — boleh aku minta *nama* kamu?")
            return Reply(text="Namanya sepertinya kurang tepat. Boleh ketik nama lengkapmu?")
        cust["nama"] = nama
        await store.set_customer(wa_number, cust)
        return Reply(text=f"Halo {cust['nama']}! Sekarang, boleh minta *alamat*-mu?")

    # Step 2: address
    if "alamat" not in cust:
        if _looks_like_question(text):
            return await _answer_then_reask(
                wa_number, text, "Lanjut ya — boleh minta *alamat* lengkapmu?")
        if not _valid_address(text):
            if len(text.split()) > 3:
                return await _answer_then_reask(
                    wa_number, text, "Lanjut ya — boleh minta *alamat* lengkapmu?")
            return Reply(text=(
                "Alamatnya belum cukup jelas buat kurir. Boleh ketik alamat lengkapnya "
                "— nama jalan, nomor rumah, dan patokan kalau ada?"
            ))
        cust["alamat"] = text.strip()
        await store.set_customer(wa_number, cust)
        return Reply(
            text="Pesananmu mau *diambil sendiri (pickup)* atau *dikirim (delivery)*?"
        )

    # Step 3: delivery method
    if "metode_pengiriman" not in cust:
        low = text.lower()
        if any(k in low for k in _KATA_PICKUP):
            cust["metode_pengiriman"] = "pickup"
        elif any(k in low for k in _KATA_DELIVERY):
            cust["metode_pengiriman"] = "delivery"
        else:
            return Reply(text="Ketik *pickup* (ambil sendiri) atau *delivery* (dikirim) ya.")
        # The contact number is the WhatsApp number the message arrived from —
        # it is already proven to work, and the backend has nowhere to store a
        # second one (customers has only nomor_wa), so asking for another one
        # meant collecting a number and then throwing it away.
        cust["nomor_hp"] = _wa_digits(wa_number)
        await store.set_customer(wa_number, cust)
        return Reply(text=_payment_type_prompt())

    # Step 4: payment type (full vs DP 50%)
    if "payment_type" not in cust:
        if not settings.allow_down_payment or _FULL_RE.search(text):
            cust["payment_type"] = "full"
        elif _DP_RE.search(text):
            cust["payment_type"] = "dp"
        else:
            return Reply(text=_payment_type_prompt())
        await store.set_customer(wa_number, cust)
        return Reply(text=_channel_prompt())

    # Step 5: payment channel (VA vs QRIS) -> finalize.
    if "channel" not in cust:
        # QRIS first: our own prompt advertises GoPay/OVO/Dana as QRIS, and a
        # customer who typed "gopay" used to get the same prompt back forever.
        if _QRIS_RE.search(text):
            cust["channel"] = "qris"
        elif _VA_RE.search(text):
            cust["channel"] = "bank_transfer"
        else:
            return Reply(text=_channel_prompt())
        await store.set_customer(wa_number, cust)
        reply_text = await checkout.finalize_order(wa_number)
        return Reply(text=reply_text)

    # Shouldn't reach here; reset to be safe.
    await store.set_state(wa_number, State.IDLE)
    return Reply(text="Ada lagi yang bisa kubantu? 😊")


def _channel_prompt() -> str:
    return (
        "Metode pembayarannya mau lewat apa?\n"
        "• Ketik *VA* — transfer bank via Virtual Account\n"
        "• Ketik *QRIS* — scan kode QR (GoPay/OVO/Dana/mobile banking)"
    )


def _payment_type_prompt() -> str:
    if settings.allow_down_payment:
        return (
            "Mau bayar *penuh* atau *DP 50%*? Ketik salah satu ya.\n"
            "(DP 50% = bayar separuh dulu sekarang)"
        )
    return "Lanjut ke pembayaran ya..."
