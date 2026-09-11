"""Tool: cancel_order — cancels a still-unpaid order on the backend (B4)."""

import logging

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import store
from app.conversation.context import get_turn_context
from app.conversation.states import State
from app.core.config import settings

logger = logging.getLogger(__name__)


def _jalur_tindak_lanjut() -> str:
    """Satu jalan yang benar-benar bisa ditempuh pelanggan, bukan 'hubungi admin'."""
    email = settings.store_support_email.strip()
    if email:
        return (f"\n\nKirim nomor pesananmu ke {email} ya, nanti tim kami yang "
                "menindaklanjuti 🙏")
    return "\n\nSampaikan nomor pesananmu ke kontak resmi Toti Cakery ya 🙏"


@tool
async def cancel_order() -> str:
    """Batalkan pesanan pelanggan yang masih pending (belum dibayar).

    Gunakan saat pelanggan minta membatalkan pesanannya.
    """
    wa = get_turn_context().wa_number
    order = await store.get_active_pending(wa)

    if order is None:
        # Only a draft cart, not a finalized order.
        await store.set_cart(wa, [])
        await store.set_state(wa, State.IDLE)
        return "Oke, draft pesanan dikosongkan. Ada lagi yang bisa kubantu?"

    try:
        await backend.cancel_order(order.order_ref)  # order_ref = backend order id
    except Exception as exc:  # noqa: BLE001 - backend returns 409 if already paid
        logger.warning("cancel failed for %s: %s", order.order_ref, exc)
        # Pesanan yang sudah dibayar hanya bisa dibatalkan Admin/Owner lewat
        # endpoint refund (JWT), dan chatbot memang tidak boleh memindahkan
        # uang. Kalimat lamanya buntu — "silakan hubungi admin" padahal
        # pelanggan tidak punya nomor admin dan bot tidak lagi menawarkan
        # sambungan untuk urusan ini. Jadi diberi jalan yang benar-benar ada.
        return (
            "Pesanan ini sudah dibayar, jadi pembatalannya tidak bisa lewat chat — "
            "harus diproses tim kami dulu supaya dananya bisa dikembalikan."
            + _jalur_tindak_lanjut()
        )

    await store.update_pending_order(order.id, status="cancelled")
    await store.set_cart(wa, [])
    await store.set_state(wa, State.IDLE)
    return "Pesanan kamu sudah dibatalkan. Terima kasih 🙏"
