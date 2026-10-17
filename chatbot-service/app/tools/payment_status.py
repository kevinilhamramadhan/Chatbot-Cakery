"""Tool: check_payment_status — "sudah saya bayar" answered from the backend.

This used to be a regex in the orchestrator that intercepted the message before
the LLM ever saw it. Routing belongs to the model, so the capability is exposed
as a tool instead: the model decides that "udah aku transfer kok" is a payment
question, and this tool supplies the one true answer.
"""

import logging

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.conversation.states import State
from app.core.config import settings
from app.tools.formatting import rupiah

logger = logging.getLogger(__name__)


async def jawab_refund(wa_number: str, lang: str) -> str | None:
    """Kabar refund pesanan terakhir, atau None kalau tidak ada yang di-refund."""
    order = await store.refund_terakhir(wa_number)
    if order is None:
        return None
    invoice = order.nomor_invoice or f"#{order.order_ref}"
    if order.status == store.MENUNGGU_TRANSFER:
        surel = settings.store_support_email.strip()
        tutup = (bahasa.teks("tutup_refund_email", lang, email=surel) if surel
                 else bahasa.teks("tutup_refund_umum", lang))
        return bahasa.teks("refund_menunggu", lang, invoice=invoice, tutup=tutup)
    if order.status == store.REFUND_OTOMATIS:
        return bahasa.teks("refund_otomatis_menunggu", lang, invoice=invoice)
    if order.status == store.KABAR_TRANSFER_TERTUNDA:
        return bahasa.teks("refund_ditransfer", lang, invoice=invoice)
    if order.status == "refunded":
        return bahasa.teks("sudah_direfund", lang)
    return None


# v11: model yang memutuskan, bukan kata kunci orchestrator, jadi tool ini juga
# menjawab sisa pelunasan dan kabar refund. Dipakai registry kalau
# INTENT_DARI_MODEL menyala; v10 tetap melihat docstring lamanya.
DESKRIPSI_V11 = """Cek pembayaran pelanggan: sudah masuk atau belum, sisa yang belum
dibayar, atau kabar uang yang dikembalikan (refund). Untuk status PESANAN pakai
`get_order_status`; untuk MELUNASI sisa DP pakai `pay_remaining`."""


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
    lang = await store.get_lang(wa)
    order = await store.get_active_pending(wa)
    if order is None:
        return await jawab_refund(wa, lang) or bahasa.teks("tak_ada_tagihan", lang)
    try:
        res = await backend.get_payment_status(order.order_ref, wa)
    except Exception as exc:  # noqa: BLE001
        logger.warning("payment status check failed for %s: %s", order.order_ref, exc)
        return bahasa.teks("cek_bayar_gagal", lang)
    inv = str((res or {}).get("invoice_status") or "").lower()
    if inv == "refunded":
        # Admin sudah me-refund lewat backend: pesanannya batal dan uangnya
        # kembali. Tanpa cabang ini pelanggan dijawab "pembayaranmu belum
        # terdeteksi" — benar secara harfiah, menyesatkan pada kenyataannya.
        await store.update_pending_order(order.id, status="refunded")
        ctx.next_state = State.IDLE
        return bahasa.teks("sudah_direfund", lang)
    if inv in ("paid", "partial"):
        await store.update_pending_order(order.id, status="paid", notified_paid=True)
        ctx.next_state = State.ORDER_ACTIVE
        teks = bahasa.teks("bayar_diterima_singkat", lang)
        sisa = (res or {}).get("amount_due") or 0
        if inv == "partial" and sisa > 0:
            teks += bahasa.teks("sisa_pelunasan", lang, sisa=rupiah(sisa))
        return teks
    return bahasa.teks("bayar_belum_terdeteksi", lang)
