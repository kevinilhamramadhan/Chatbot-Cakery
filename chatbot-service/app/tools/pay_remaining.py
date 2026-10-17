"""Tool: pay_remaining — terbitkan tagihan pelunasan untuk pesanan DP.

Backend menerima pelunasan sebagai tagihan `full` sebesar sisa invoice, dan
membatalkan pesanan DP yang belum lunas pada H-1 pukul 18.00 WIB — tanpa jalur
ini pelanggan WhatsApp tidak punya cara melunasinya. Dulu dicegat kata kunci di
orchestrator ("lunasi"); sejak v11 model yang memutuskan, jadi "mau bayar
kekurangannya" juga sampai ke sini.
"""

import json
import logging

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import bahasa, checkout, store
from app.conversation.context import get_turn_context
from app.tools.formatting import rupiah

logger = logging.getLogger(__name__)


async def lunasi(wa_number: str, lang: str) -> str | None:
    """Teks tagihan pelunasan; None = tidak ada pesanan DP yang bisa dilunasi."""
    order = await store.get_active_pending(wa_number)
    if order is None or order.status == "pending":
        return None
    if order.payment_type not in ("dp", store.PELUNASAN):
        return bahasa.teks("pesanan_sudah_lunas", lang,
                           invoice=order.nomor_invoice or f"#{order.order_ref}")
    invoice = order.nomor_invoice or f"#{order.order_ref}"
    try:
        res = await backend.get_payment_status(order.order_ref, wa_number)
        sisa = float((res or {}).get("amount_due") or 0)
        if sisa <= 0:
            return bahasa.teks("pesanan_sudah_lunas", lang, invoice=invoice)
        channel = json.loads(order.customer_json or "{}").get("channel", "bank_transfer")
        pay = await backend.create_payment(order.order_ref, sisa, channel=channel,
                                           payment_type="full", wa_number=wa_number)
    except Exception as exc:  # noqa: BLE001
        logger.exception("tagihan pelunasan %s gagal: %s", order.order_ref, exc)
        return bahasa.teks("pelunasan_gagal", lang)
    va, qris = pay.get("va_number"), pay.get("qris_url")
    if not va and not qris:
        logger.error("tagihan pelunasan %s tanpa cara bayar", order.order_ref)
        return bahasa.teks("pelunasan_gagal", lang)
    cara = f"💳 Virtual Account: *{va}*" if va else bahasa.teks("scan_qris", lang)
    await store.update_pending_order(
        order.id, payment_type=store.PELUNASAN, amount_due=sisa, pay_instruction=cara,
        qris_url=None if va else qris, payment_ref=pay.get("pg_transaction_id"))
    if not va:
        checkout.kirim_gambar_qris(qris, lang)
    return bahasa.teks("tagihan_pelunasan", lang, invoice=invoice, jumlah=rupiah(sisa),
                       cara_bayar=cara)


@tool
async def pay_remaining() -> str:
    """Kirim tagihan pelunasan sisa DP. Gunakan saat pelanggan ingin melunasi
    atau bertanya cara melunasi pesanan yang baru dibayar DP-nya."""
    wa = get_turn_context().wa_number
    lang = await store.get_lang(wa)
    jawab = await lunasi(wa, lang)
    if jawab:
        return jawab
    order = await store.get_active_pending(wa)
    if order is None:
        return bahasa.teks("tak_ada_tagihan", lang)
    # Belum dibayar sama sekali: yang ditagih tagihan pertamanya, bukan pelunasan.
    return bahasa.teks("tagihan_belum_dibayar", lang,
                       invoice=order.nomor_invoice or f"#{order.order_ref}",
                       jumlah=rupiah(order.amount_due), total=rupiah(order.total_amount))
