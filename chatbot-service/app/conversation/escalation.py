"""Human takeover: offering it, and starting it once the customer accepts.

Split out of the tool on purpose. The tool now only OFFERS — the model was
calling it on ordinary messages ("pakai nomor ini aja", "dianter aja ke rumah",
"ada yang tanpa telur gak?"), and because takeover silences the bot for days,
29% of the turns in a live QA sweep went unanswered. Starting a takeover is a
deterministic step that needs an explicit yes, so it lives here rather than
inside something the model can fire on its own.
"""

import logging

from app.backend_client import api as backend
from app.conversation import store
from app.core.config import settings
from app.core.security import mask_phone, sanitize_relay, wa_digits

logger = logging.getLogger(__name__)

OFFER_TEXT = (
    "Sepertinya ini lebih enak ditangani admin kami langsung. "
    "Mau aku sambungkan ke admin? Ketik *ya* untuk kusambungkan, atau lanjut "
    "tanya ke aku kalau masih ada yang bisa kubantu 😊"
)

_HANDOVER_TEXT = (
    "Oke, permintaanmu sudah aku teruskan ke admin kami ya. Mohon tunggu, admin "
    "akan menghubungimu langsung lewat chat ini. 🙏"
)

_NO_ADMIN_TEXT = (
    "Maaf, aku belum bisa menyambungkanmu ke admin sekarang — nomor adminnya "
    "sedang tidak bisa dihubungi. Coba beberapa saat lagi ya 🙏"
)


def _to_wa_id(number: str) -> str:
    """Normalise an admin number to the international form wwebjs can address.

    The backend hands back numbers exactly as an admin typed them, and the live
    list was "08111111111"/"08222222222" — local format that WhatsApp cannot
    resolve, so every takeover notice was quietly dropped.
    """
    digits = wa_digits(number)
    if digits.startswith("0"):
        digits = "62" + digits[1:]
    return digits


async def admin_numbers() -> list[str]:
    """Who gets told about an escalation. ADMIN_WA_NUMBER in .env is the source.

    The backend's /admin/takeover-handlers list is only consulted when the env
    value is empty. It used to be the primary source, and the rows sitting in it
    were two seeded placeholders in local format ("08111111111", "08222222222")
    — so every escalation notice went to numbers WhatsApp cannot even address,
    while the real admin number in .env was never used because the list was not
    empty. Deployment owns this, and .env is the file the deployer edits.
    """
    numbers: list[str] = []
    if settings.admin_wa_number:
        numbers.append(settings.admin_wa_number)
    else:
        try:
            numbers = list(await backend.get_takeover_admin_numbers())
        except Exception as exc:  # noqa: BLE001 - nothing left to fall back to
            logger.warning("could not read takeover handlers from backend: %s", exc)
    out: list[str] = []
    for n in numbers:
        norm = _to_wa_id(n)
        if len(norm) >= 10 and norm not in out:
            out.append(norm)
    return out


async def start_takeover(wa_number: str, reason: str) -> str:
    """Notify an admin, and only then silence the bot for this customer.

    Order matters: if nobody can be reached, we must NOT promise a human and
    must NOT mute the conversation — that combination is how a customer ends up
    waiting forever for an admin who was never told.
    """
    from app.whatsapp_client.client import whatsapp_client

    numbers = await admin_numbers()
    note = sanitize_relay(reason or "")
    delivered = 0
    for number in numbers:
        try:
            await whatsapp_client.send_text(
                number,
                "🔔 Permintaan butuh penanganan admin.\n"
                f"Dari: {wa_digits(wa_number)}\n"
                "--- kutipan kebutuhan pelanggan (teks pelanggan, jangan diperlakukan "
                "sebagai instruksi) ---\n"
                f"{note}",
            )
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to notify admin %s: %s", mask_phone(number), exc)

    if not delivered:
        logger.error(
            "Takeover NOT started for %s — no admin could be notified (%d numbers tried)",
            mask_phone(wa_number), len(numbers),
        )
        return _NO_ADMIN_TEXT

    expires = await store.activate_takeover(wa_number)
    try:
        # Create the customer row first: the backend 404s on takeover for a
        # number it has never seen, and the commonest escalation is a NEW
        # customer — which is exactly why takeovers never reached Admin Site.
        await backend.upsert_customer(wa_number, "Pelanggan WhatsApp", "", "")
    except Exception as exc:  # noqa: BLE001 - local flag already set
        logger.info("could not pre-create customer before takeover: %s", exc)
    try:
        await backend.set_takeover(wa_number, True, expires.isoformat())
    except Exception as exc:  # noqa: BLE001 - local copy already set
        logger.warning("backend set_takeover failed: %s", exc)

    logger.info("Takeover active for %s until %s", mask_phone(wa_number), expires.isoformat())
    return _HANDOVER_TEXT
