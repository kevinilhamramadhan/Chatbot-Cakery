"""Tool: cancel_order — cancels a still-unpaid order on the backend (B4)."""

import json
import logging

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.conversation.states import State, text_is_cancel
from app.core.config import settings

logger = logging.getLogger(__name__)


def _jalur_tindak_lanjut(lang: str) -> str:
    """Satu jalan yang benar-benar bisa ditempuh pelanggan, bukan 'hubungi admin'."""
    email = settings.store_support_email.strip()
    if email:
        return bahasa.teks("tindak_lanjut_email", lang, email=email)
    return bahasa.teks("tindak_lanjut_umum", lang)


@tool
async def cancel_order() -> str:
    """Batalkan pesanan pelanggan.

    Gunakan saat pelanggan minta membatalkan pesanannya. Pesanan yang belum
    dibayar langsung dibatalkan; yang sudah dibayar (DP maupun lunas) dijawab
    dengan pertanyaan konfirmasi lebih dulu, karena pembatalannya sekalian
    dengan pengembalian dana.
    """
    ctx = get_turn_context()
    wa = ctx.wa_number
    lang = await store.get_lang(wa)
    # Penjaga data: model pernah memanggil tool ini untuk pesan "order" saja.
    # Membatalkan tanpa kata batal dari pelanggan terlalu mahal kalau salah.
    if ctx.user_text and not text_is_cancel(ctx.user_text):
        return bahasa.teks("bukan_niat_batal", lang)
    order = await store.get_active_pending(wa)

    if order is None:
        # Only a draft cart, not a finalized order.
        await store.set_cart(wa, [])
        await store.set_state(wa, State.IDLE)
        return bahasa.teks("draft_dikosongkan", lang)

    # SETIAP pembatalan pesanan ditanyakan dulu — bukan hanya yang sudah dibayar.
    # Pesanan sudah masuk ke backend dan ke Admin Site, jadi menghapusnya karena
    # satu kalimat yang bisa saja salah baca terlalu mahal. Draft keranjang yang
    # belum jadi pesanan tetap langsung dikosongkan (lihat di atas): tidak ada
    # yang hilang di sisi toko.
    sudah_dibayar = order.status in ("paid", "ready")
    if sudah_dibayar and not await _masih_boleh_refund(wa):
        # Kebijakan toko: begitu admin memindahkan pesanan ke "sedang diproses",
        # kuenya sudah dikerjakan dan pembatalan mandiri tidak berlaku lagi.
        return _teks_sudah_dikerjakan(lang)

    get_turn_context().next_state = State.AWAITING_CANCEL_CONFIRMATION
    return konfirmasi_batal(order, sudah_dibayar, lang)


def _teks_sudah_dikerjakan(lang: str) -> str:
    alamat = settings.store_support_email.strip() or bahasa.teks("kontak_resmi", lang)
    return bahasa.teks("sudah_dikerjakan", lang, alamat=alamat)


async def _masih_boleh_refund(wa_number: str) -> bool:
    """Hanya pesanan berstatus `pending` di backend yang boleh dibatalkan sendiri.

    Perpindahan `pending` -> `in_process` dilakukan admin secara manual, dan
    itulah penanda bahwa kuenya mulai dikerjakan. Kalau status tidak terbaca
    (backend sedang tidak bisa dihubungi), pelanggan tetap ditanya — keputusan
    akhirnya toh ada di backend saat pembatalan dieksekusi.
    """
    try:
        o = await backend.get_latest_order(wa_number)
    except Exception as exc:  # noqa: BLE001
        logger.info("status pesanan tidak terbaca saat cek refund: %s", exc)
        return True
    if not o:
        return True
    return str(o.get("status") or "").lower() == "pending"


def _bayar_pakai_va(order) -> bool:
    """Pembayaran lewat VA tidak bisa dikembalikan otomatis oleh Midtrans.

    Metode yang mendukung refund otomatis: QRIS (semua e-wallet dan m-banking
    lewat satu kode) dan kartu. Transfer VA tidak, jadi dananya dikembalikan
    manual oleh tim toko — itu harus dikatakan sejak awal, bukan sesudah
    pelanggan menjawab ya.
    """
    try:
        cust = json.loads(order.customer_json or "{}")
    except (TypeError, ValueError):
        return False
    return str(cust.get("channel") or "") == "bank_transfer"


def konfirmasi_batal(order, sudah_dibayar: bool, lang: str | None = None) -> str:
    """Pertanyaan tertutup sebelum pesanan benar-benar dibatalkan."""
    label = order.nomor_invoice or f"#{order.order_ref}"
    if sudah_dibayar:
        cara = bahasa.teks("cara_refund_transfer" if _bayar_pakai_va(order)
                           else "cara_refund_otomatis", lang)
        return bahasa.teks("konfirmasi_batal_berbayar", lang, label=label, cara=cara)
    return bahasa.teks("konfirmasi_batal", lang, label=label)


async def proses_pembatalan(wa_number: str) -> str:
    """Jalankan pembatalan sesudah pelanggan menjawab ya.

    Dua jalur dicoba karena backend boleh memilih bentuknya: endpoint cancel
    yang sudah ada (kalau nanti diizinkan menangani pesanan berbayar), lalu
    endpoint refund. Selama keduanya menolak, pelanggan tidak dibiarkan buntu —
    dia diberi alamat email yang benar-benar ditangani orang.
    """
    lang = await store.get_lang(wa_number)
    order = await store.get_active_pending(wa_number)
    if order is None:
        await store.set_state(wa_number, State.IDLE)
        return bahasa.teks("tidak_ada_yang_dibatalkan", lang)

    sudah_dibayar = order.status in ("paid", "ready")
    berhasil = False
    mode = ""
    try:
        await backend.cancel_order(order.order_ref)
        berhasil = True
    except Exception:  # noqa: BLE001 - 409 kalau pesanannya sudah dibayar
        hasil = await backend.refund_order(
            order.order_ref, "Dibatalkan pelanggan lewat chatbot", wa_number)
        berhasil = hasil is not None
        # Backend menandai apakah Midtrans benar-benar mengembalikan dananya
        # ("auto") atau cuma dicatat dan uangnya ditransfer orang ("manual").
        # Bedanya nyata untuk pelanggan: yang manual butuh beberapa hari kerja.
        mode = str((hasil or {}).get("refund_mode") or "")

    if not berhasil:
        logger.info("refund pelanggan belum bisa otomatis untuk %s", order.order_ref)
        return bahasa.teks("batal_perlu_tim", lang) + _jalur_tindak_lanjut(lang)

    # Refund manual belum selesai saat ini juga — uangnya baru berpindah setelah
    # admin mentransfernya. Barisnya disimpan dengan status menunggu supaya
    # kabar "dana sudah kami transfer" nanti masih punya sasaran.
    manual = mode == "manual" or (not mode and _bayar_pakai_va(order))
    await store.update_pending_order(
        order.id, status=store.MENUNGGU_TRANSFER if (manual and sudah_dibayar)
        else "cancelled")
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.IDLE)
    if not sudah_dibayar:
        # Belum ada uang yang masuk — jangan menjanjikan pengembalian dana.
        return bahasa.teks("batal_belum_bayar", lang)
    if manual:
        email = settings.store_support_email.strip()
        tutup = (bahasa.teks("tutup_refund_email", lang, email=email) if email
                 else bahasa.teks("tutup_refund_umum", lang))
        return bahasa.teks("batal_refund_manual", lang, tutup=tutup)
    return bahasa.teks("batal_refund_otomatis", lang)
