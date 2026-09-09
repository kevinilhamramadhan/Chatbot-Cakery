"""Tool: kirim_ulang_pembayaran — repeats the payment instructions.

The QRIS link and VA number were sent exactly once, at checkout. On WhatsApp
that message is buried within minutes, and the customer only has 30 minutes to
pay; "kode qr nya kirim ulang dong" came back as a bare order status with no way
to pay. The instructions are snapshotted on the pending order at checkout, so
repeating them costs nothing and never invents a second charge.
"""

from langchain_core.tools import tool

from app.conversation import store
from app.conversation.context import get_turn_context
from app.core.config import settings
from app.tools.formatting import rupiah


@tool
async def kirim_ulang_pembayaran() -> str:
    """Kirim ulang info pembayaran (QRIS/Virtual Account) pesanan yang belum dibayar.

    Gunakan saat pelanggan minta dikirimkan ulang kode QR, nomor VA, tautan
    pembayaran, atau bertanya bagaimana cara membayar pesanannya.
    """
    order = await store.get_active_pending(get_turn_context().wa_number)
    if order is None:
        return "Saat ini kamu belum punya tagihan yang menunggu pembayaran 😊"
    if order.status != "pending":
        return (f"Pesanan *{order.nomor_invoice or order.order_ref}* sudah dibayar — "
                "tidak ada tagihan yang perlu dibayar lagi 🙏")
    if not order.pay_instruction:
        # Older rows (before this snapshot existed) and any charge that came
        # back without an instrument. Say so instead of sending an empty line.
        return (
            f"Maaf, aku tidak bisa mengambil ulang info pembayaran untuk "
            f"*{order.nomor_invoice or order.order_ref}*. Ketik *batal* lalu pesan "
            "ulang ya, nanti tagihannya kuterbitkan lagi 🙏"
        )
    label = "DP 50%" if order.payment_type == "dp" else "Pembayaran penuh"
    return (
        f"Ini lagi ya untuk pesanan *{order.nomor_invoice or order.order_ref}*:\n\n"
        f"{label} yang harus dibayar: *{rupiah(order.amount_due)}*\n\n"
        f"{order.pay_instruction}\n\n"
        f"Batas waktu pembayaran {settings.payment_timeout_minutes} menit sejak "
        "pesanan dibuat. Ketik *batal* kalau ingin membatalkan."
    )
