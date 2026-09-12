"""Tool: get_order_status — reads the latest order from the backend (B3)."""

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation.context import get_turn_context
from app.tools.formatting import rupiah

_ORDER = {"pending": "Menunggu pembayaran", "in_process": "Sedang diproses",
          "ready": "Siap diambil/dikirim", "delivered": "Dikirim",
          "picked_up": "Sudah diambil", "cancelled": "Dibatalkan",
          "refunded": "Dibatalkan, dana sudah dikembalikan"}
_INV = {"unpaid": "belum dibayar", "partial": "DP terbayar", "paid": "lunas", "refunded": "dikembalikan"}


@tool
async def get_order_status() -> str:
    """Cek status pesanan terakhir pelanggan. Gunakan saat pelanggan menanyakan
    progress atau status pesanannya.
    """
    wa = get_turn_context().wa_number
    try:
        o = await backend.get_latest_order(wa)
    except Exception:  # noqa: BLE001
        return "Maaf, status pesanan lagi tidak bisa diambil. Coba lagi sebentar ya 🙏"
    if not o:
        return "Saat ini kamu belum punya pesanan yang sedang berjalan."

    inv = o.get("invoice") or {}
    nomor = inv.get("nomor_invoice") or f"#{o.get('id')}"
    order_lbl = _ORDER.get(o.get("status"), o.get("status"))
    inv_lbl = _INV.get(inv.get("status"), inv.get("status"))
    items = o.get("items") or []
    return (
        f"Status pesanan *{nomor}*: {order_lbl} (pembayaran: {inv_lbl})\n"
        f"Jumlah item: {len(items)}\n"
        f"Total: {rupiah(o.get('total_harga_pesanan'))}"
    )
