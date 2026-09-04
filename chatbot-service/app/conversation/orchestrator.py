"""Main conversation brain: routes an inbound WhatsApp message to a reply.

Handles human-takeover suppression, the deterministic order flow (cart confirm ->
identity -> payment-type -> checkout), and delegates open-ended turns to the LLM
agent. Returns a Reply; the caller is responsible for actually sending it.
"""

import logging
import re
from dataclasses import dataclass, field

from app.backend_client import api as backend
from app.conversation import checkout, store, verification
from app.conversation.context import OutboundMedia, TurnContext, set_turn_context
from app.conversation.states import State, text_is_cancel, text_is_confirm
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


# ── Identity validation ───────────────────────────────────────────────────────
def _valid_name(s: str) -> bool:
    return len(s.strip()) >= 2 and not s.strip().isdigit()


def _valid_address(s: str) -> bool:
    """Length alone let "rumah" through and a real order was created for it."""
    t = s.strip()
    if len(t) < 10:
        return False
    return bool(re.search(r"\d", t) or _ADDRESS_HINT_RE.search(t))


def _valid_phone(s: str) -> bool:
    d = _wa_digits(s)
    return 8 <= len(d) <= 15


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

    if state == State.AWAITING_CART_CONFIRMATION:
        reply = await _handle_confirmation(wa_number, text)
    elif state == State.COLLECTING_IDENTITY:
        reply = await _handle_identity(wa_number, text)
    else:
        # IDLE / AWAITING_PAYMENT / ORDER_ACTIVE -> LLM agent (with tools).
        reply = await _run_agent_turn(wa_number, text)

    if reply.text:
        await store.log_message(wa_number, "out", reply.text)
    return reply


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


async def _answer_then_reask(wa_number: str, text: str, reask: str) -> Reply:
    """Answer an off-script question, then repeat the step we were standing on.

    State is pinned back afterwards: the identity steps are deterministic, so a
    tool that tried to move the state (add_to_cart) must not win here.
    """
    reply = await _run_agent_turn(wa_number, text)
    await store.set_state(wa_number, State.COLLECTING_IDENTITY)
    body = (reply.text or "").strip()
    return Reply(text=f"{body}\n\n{reask}" if body else reask, media=reply.media)


async def _run_agent_turn(wa_number: str, text: str) -> Reply:
    ctx = TurnContext(wa_number=wa_number)
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

    if text_is_confirm(text):
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

    # Otherwise treat as a modification / other request -> agent (can add items).
    return await _run_agent_turn(wa_number, text)


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
        if not _valid_name(text):
            return Reply(text="Namanya sepertinya kurang tepat. Boleh ketik nama lengkapmu?")
        cust["nama"] = text.strip()
        await store.set_customer(wa_number, cust)
        return Reply(text=f"Halo {cust['nama']}! Sekarang, boleh minta *alamat*-mu?")

    # Step 2: address
    if "alamat" not in cust:
        if _looks_like_question(text):
            return await _answer_then_reask(
                wa_number, text, "Lanjut ya — boleh minta *alamat* lengkapmu?")
        if not _valid_address(text):
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
        if "pickup" in low or "ambil" in low:
            cust["metode_pengiriman"] = "pickup"
        elif "delivery" in low or "kirim" in low or "antar" in low:
            cust["metode_pengiriman"] = "delivery"
        else:
            return Reply(text="Ketik *pickup* (ambil sendiri) atau *delivery* (dikirim) ya.")
        await store.set_customer(wa_number, cust)
        # Phone step: auto-fill suggestion from WA number (PROMPT decision).
        if settings.autofill_phone_from_wa:
            return Reply(
                text=(
                    f"Aku pakai nomor WA ini sebagai kontak: *{_wa_digits(wa_number)}*.\n"
                    "Ketik *ya* untuk pakai nomor ini, atau ketik nomor HP lain."
                )
            )
        return Reply(text="Terakhir, boleh minta *nomor HP* aktifmu?")

    # Step 4: phone (auto-fill on confirm, else validate typed number)
    if "nomor_hp" not in cust:
        if settings.autofill_phone_from_wa and text_is_confirm(text):
            cust["nomor_hp"] = _wa_digits(wa_number)
        elif _valid_phone(text):
            cust["nomor_hp"] = _wa_digits(text)
        else:
            return Reply(
                text="Nomor HP-nya kurang valid (harus 8-15 digit angka). Coba ketik ulang ya."
            )
        await store.set_customer(wa_number, cust)
        return Reply(text=_payment_type_prompt())

    # Step 5: payment type (full vs DP 50%)
    if "payment_type" not in cust:
        if not settings.allow_down_payment or _FULL_RE.search(text):
            cust["payment_type"] = "full"
        elif _DP_RE.search(text):
            cust["payment_type"] = "dp"
        else:
            return Reply(text=_payment_type_prompt())
        await store.set_customer(wa_number, cust)
        return Reply(text=_channel_prompt())

    # Step 6: payment channel (VA vs QRIS) -> finalize.
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
