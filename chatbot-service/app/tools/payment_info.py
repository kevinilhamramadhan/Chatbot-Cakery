"""Tool: resend_payment_method — repeats the payment instructions.

The QRIS link and VA number were sent exactly once, at checkout. On WhatsApp
that message is buried within minutes, and the customer only has 30 minutes to
pay; "kode qr nya kirim ulang dong" came back as a bare order status with no way
to pay. The instructions are snapshotted on the pending order at checkout, so
repeating them costs nothing and never invents a second charge.
"""

from langchain_core.tools import tool

from app.conversation import bahasa, checkout, store
from app.conversation.context import get_turn_context
from app.core.config import settings
from app.tools.formatting import rupiah


@tool
async def resend_payment_method() -> str:
    """Kirim ulang info pembayaran (QRIS/Virtual Account) pesanan yang belum dibayar.

    Gunakan saat pelanggan minta dikirimkan ulang kode QR, nomor VA, tautan
    pembayaran, atau bertanya bagaimana cara membayar pesanannya.
    """
    wa = get_turn_context().wa_number
    lang = await store.get_lang(wa)
    order = await store.get_active_pending(wa)
    if order is None:
        return bahasa.teks("tak_ada_tagihan_menunggu", lang)
    invoice = order.nomor_invoice or order.order_ref
    if order.status != "pending":
        return bahasa.teks("tagihan_sudah_dibayar", lang, invoice=invoice)
    if not order.pay_instruction:
        # Older rows (before this snapshot existed) and any charge that came
        # back without an instrument. Say so instead of sending an empty line.
        return bahasa.teks("info_bayar_tak_terambil", lang, invoice=invoice)
    # Gambar QR-nya ikut dikirim ulang, bukan cuma kalimatnya: pesanan QRIS yang
    # dibuat sebelum kolom ini ada tidak punya URL-nya, dan itu tidak apa-apa --
    # pay_instruction lama masih memuat tautannya apa adanya.
    if order.qris_url:
        checkout.kirim_gambar_qris(order.qris_url, lang)
    return bahasa.teks(
        "kirim_ulang_bayar", lang, invoice=invoice,
        label=bahasa.teks("label_dp" if order.payment_type == "dp" else "label_bayar_penuh", lang),
        jumlah=rupiah(order.amount_due), cara_bayar=order.pay_instruction,
        menit=settings.payment_timeout_minutes)
