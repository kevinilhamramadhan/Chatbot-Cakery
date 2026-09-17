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
from app.conversation import bahasa, rbac, store
from app.core.security import mask_phone, sanitize_relay, wa_digits

logger = logging.getLogger(__name__)

# Teks tawaran dipakai juga oleh orchestrator untuk mengukur umur tawarannya
# (dicocokkan dengan awalan pesan di riwayat), jadi versi Indonesianya tetap
# dipegang sebagai konstanta — bukan cuma dipanggil lewat templat.
OFFER_TEXT = bahasa.teks("tawaran_admin", bahasa.ID)


def teks_tawaran(lang: str) -> str:
    return bahasa.teks("tawaran_admin", lang)


async def admin_numbers() -> list[str]:
    """Siapa yang diberi tahu saat ada eskalasi — dari direktori peran.

    Isinya user aktif ber-`handles_takeover` di basis data backend, jadi Admin
    Site adalah satu-satunya tempat mengaturnya dan tidak perlu deploy ulang
    untuk mengganti siapa yang bertugas. Sengaja tidak ada cadangan di .env: dua
    sumber untuk "siapa adminnya" adalah cara paling rapi untuk memakai yang
    salah.

    Penyaringnya flag, BUKAN peran — Owner yang bertugas menerima eskalasi sama
    seperti Admin, dan di produksi satu dari dua penerimanya memang Owner.
    Lihat app/conversation/rbac.py.
    """
    return await rbac.nomor_penerima_takeover()


async def start_takeover(wa_number: str, reason: str, lang: str | None = None) -> str:
    """Notify an admin, and only then silence the bot for this customer.

    Order matters: if nobody can be reached, we must NOT promise a human and
    must NOT mute the conversation — that combination is how a customer ends up
    waiting forever for an admin who was never told.
    """
    from app.whatsapp_client.client import whatsapp_client

    if lang is None:
        lang = await store.get_lang(wa_number)
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
        return bahasa.teks("admin_tidak_tersedia", lang)

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
    return bahasa.teks("diteruskan_ke_admin", lang)
