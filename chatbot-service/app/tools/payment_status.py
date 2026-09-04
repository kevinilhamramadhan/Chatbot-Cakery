"""Tool: check_payment_status — "sudah saya bayar" answered from the backend.

This used to be a regex in the orchestrator that intercepted the message before
the LLM ever saw it. Routing belongs to the model, so the capability is exposed
as a tool instead: the model decides that "udah aku transfer kok" is a payment
question, and this tool supplies the one true answer.
"""

import logging

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import store
from app.conversation.context import get_turn_context
from app.conversation.states import State

logger = logging.getLogger(__name__)


@tool
async def check_payment_status() -> str:
    """Cek apakah pembayaran pelanggan sudah masuk.

    Gunakan saat pelanggan menyatakan atau menanyakan bahwa dia SUDAH membayar
    ("sudah aku transfer", "udah bayar kok", "sudah scan QRIS-nya", "pembayaran
    aku sudah masuk belum?"). Untuk pertanyaan status PESANAN (sudah siap/belum,
    sedang diproses) pakai `get_order_status`.
    """
    ctx = get_turn_context()
    wa = ctx.wa_number
    order = await store.get_active_pending(wa)
    if order is None:
        return ("Aku tidak menemukan pesanan yang menunggu pembayaran. "
                "Mau lihat menu dulu? 😊")
    try:
        res = await backend.get_payment_status(order.order_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment status check failed for %s: %s", order.order_ref, exc)
        return ("Maaf, status pembayaran belum bisa kucek sekarang. "
                "Coba tanya lagi sebentar lagi ya 🙏")
    if (res or {}).get("invoice_status") in ("paid", "partial"):
        await store.update_pending_order(order.id, status="paid", notified_paid=True)
        ctx.next_state = State.ORDER_ACTIVE
        return ("Pembayaran sudah kami terima ✅\n"
                "Pesananmu akan segera kami proses. Terima kasih! 🎂")
    return ("Pembayaranmu belum terdeteksi di sistem kami. Biasanya butuh 1-2 menit "
            "setelah transfer atau scan berhasil — nanti aku kabari otomatis begitu "
            "masuk ya 🙏")
