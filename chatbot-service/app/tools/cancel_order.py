"""Tool: cancel_order — cancels a still-unpaid order on the backend (B4)."""

import json
import logging
from datetime import datetime, timezone

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.conversation.states import State, text_menyebut_batal
from app.tools.formatting import rupiah

logger = logging.getLogger(__name__)


async def _tawarkan_admin(wa_number: str, lang: str, alasan: str) -> str:
    """Tindak lanjut lewat admin di chat ini: ditawarkan, tersambung setelah "ya".

    Keputusan Kevin (5 Nov 2026): chatbot tidak lagi mengarahkan pelanggan ke
    email toko; alasannya ikut terkirim ke admin kalau pelanggan setuju.
    """
    await store.set_pending_escalation(wa_number, alasan)
    return "\n\n" + bahasa.teks("tawaran_admin_lanjut", lang)


@tool
async def cancel_order() -> str:
    """Batalkan pesanan pelanggan.

    Gunakan saat pelanggan minta membatalkan pesanannya. Pesanan yang belum
    dibayar dibatalkan setelah dikonfirmasi; yang sudah dibayar (DP maupun
    lunas) diarahkan ke admin, karena pengembalian dananya ditangani admin.
    """
    ctx = get_turn_context()
    wa = ctx.wa_number
    lang = await store.get_lang(wa)
    # Penjaga data: model pernah memanggil tool ini untuk pesan "order" saja.
    # Membatalkan tanpa kata batal dari pelanggan terlalu mahal kalau salah.
    if ctx.user_text and not text_menyebut_batal(ctx.user_text):
        return bahasa.teks("bukan_niat_batal", lang)
    order = await store.get_active_pending(wa)

    if order is None:
        # "bolu pandannya ga jadi" padahal tidak ada apa pun (kuenya tadi habis,
        # jadi tidak pernah masuk keranjang): bukan "draft dikosongkan".
        if not await store.get_cart(wa):
            return bahasa.teks("tidak_jadi_kosong", lang)
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
    if sudah_dibayar:
        return await _batal_berbayar_ke_admin(wa, lang, order)

    get_turn_context().next_state = State.AWAITING_CANCEL_CONFIRMATION
    return konfirmasi_batal(order, sudah_dibayar, lang)


async def _batal_berbayar_ke_admin(wa_number: str, lang: str, order) -> str:
    """Pesanan yang sudah dibayar tidak dibatalkan chatbot sendiri.

    Keputusan Kevin (19 Nov 2026): setiap pengembalian dana ditangani admin
    lewat takeover. Admin memutuskan dan mengerjakan refund-nya dari Seller
    Site; kabar "dana sudah dikembalikan" tetap dikirim chatbot begitu backend
    menandai pesanannya refunded (notify_refunded / _check_refunded).
    """
    if not await _masih_boleh_refund(wa_number):
        # Kebijakan toko: begitu admin memindahkan pesanan ke "sedang diproses",
        # kuenya sudah dikerjakan dan pembatalan mandiri tidak berlaku lagi.
        return bahasa.teks("sudah_dikerjakan", lang) + await _tawarkan_admin(
            wa_number, lang, f"Pelanggan ingin membatalkan pesanan {_label(order)} yang "
                             "sudah dikerjakan")
    return bahasa.teks("batal_berbayar_ke_admin", lang, label=_label(order)) + \
        await _tawarkan_admin(
            wa_number, lang, f"Pelanggan ingin membatalkan pesanan {_label(order)} yang "
                             "sudah dibayar dan minta refund")


_PESANAN_TUTUP = {"cancelled", "refunded", "cancelled_settlement_expired"}


async def arahkan_refund(wa_number: str, lang: str) -> str:
    """Pelanggan minta refund: diarahkan ke admin takeover, tidak diproses sendiri.

    Berlaku untuk pesanan dari mana pun. Pesanan WhatsApp dibaca dari baris
    lokalnya; pesanan website (tidak punya baris lokal) dari pesanan terakhir
    nomor ini di backend. Takeover tetap menunggu "ya" seperti tawaran admin
    lainnya, dan alasannya (invoice, nominal, status) ikut terkirim ke admin.
    """
    from app.tools.payment_status import jawab_refund

    # Refund yang sudah berjalan dijawab statusnya, tidak diajukan dua kali.
    berjalan = await jawab_refund(wa_number, lang)
    if berjalan:
        return berjalan

    lokal = await store.get_active_pending(wa_number)
    if lokal is not None:
        if lokal.status not in ("paid", "ready"):
            return bahasa.teks("refund_belum_bayar", lang, label=_label(lokal))
        return bahasa.teks("refund_lewat_admin", lang, label=_label(lokal)) + \
            await _tawarkan_admin(
                wa_number, lang, f"Pelanggan minta refund pesanan {_label(lokal)} "
                                 f"(tagihan terbayar {rupiah(lokal.amount_due)})")

    try:
        o = await backend.get_latest_order(wa_number)
    except Exception as exc:  # noqa: BLE001
        logger.info("pesanan terakhir tidak terbaca saat minta refund: %s", exc)
        o = None
    inv = (o or {}).get("invoice") or {}
    status_inv = str(inv.get("status") or (o or {}).get("payment_status") or "").lower()
    status_pesanan = str((o or {}).get("status") or "").lower()
    label = inv.get("nomor_invoice") or (f"#{o.get('id')}" if o else "")

    if o and status_pesanan not in _PESANAN_TUTUP and status_inv in ("paid", "partial"):
        dibayar = (o or {}).get("amount_paid")
        nominal = f", sudah dibayar {rupiah(dibayar)}" if dibayar else ""
        return bahasa.teks("refund_lewat_admin", lang, label=label) + await _tawarkan_admin(
            wa_number, lang, f"Pelanggan minta refund pesanan {label} "
                             f"(status {status_pesanan or '-'}{nominal})")
    if o and status_pesanan == "pending" and status_inv == "unpaid":
        return bahasa.teks("refund_belum_bayar", lang, label=label)
    # Tidak ada pesanan berbayar atas nomor ini (mungkin dibayar dari nomor
    # lain): tetap disambungkan, admin yang mencocokkan datanya.
    return bahasa.teks("refund_tanpa_pesanan", lang) + await _tawarkan_admin(
        wa_number, lang, "Pelanggan minta refund, tapi tidak ada pesanan berbayar "
                         "yang aktif atas nomor ini")


def _label(order) -> str:
    return order.nomor_invoice or f"#{order.order_ref}"


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

    Hanya pesanan yang belum dibayar yang dibatalkan di sini. Pesanan yang
    sudah dibayar (misalnya pembayarannya masuk di antara pertanyaan dan
    jawaban "ya") diarahkan ke admin, sama seperti di cancel_order.
    """
    lang = await store.get_lang(wa_number)
    order = await store.get_active_pending(wa_number)
    if order is None:
        await store.set_state(wa_number, State.IDLE)
        return bahasa.teks("tidak_ada_yang_dibatalkan", lang)

    if order.status in ("paid", "ready"):
        # Pertanyaan "yakin batal?" sudah terjawab; tetap di keadaan itu membuat
        # pesan berikutnya dibaca sebagai jawaban ya/tidak lagi.
        await store.set_state(wa_number, State.ORDER_ACTIVE)
        return await _batal_berbayar_ke_admin(wa_number, lang, order)

    try:
        await backend.cancel_order(order.order_ref, order.wa_number)
    except Exception:  # noqa: BLE001
        logger.info("pembatalan belum bisa otomatis untuk %s", order.order_ref)
        await store.set_state(wa_number, State.AWAITING_PAYMENT)
        # Belum ada uang masuk: jangan menjanjikan pengembalian dana. Sejak
        # backend d46c973 pesanan dengan transaksi Midtrans pending dibatalkan
        # di gateway dulu; 409/502 tersisa hanya bila gateway gagal atau
        # hasilnya belum pasti. Tagihan yang tidak dibayar tetap dibatalkan
        # worker backend saat batasnya lewat.
        label = order.nomor_invoice or f"#{order.order_ref}"
        batas = order.expires_at if order.expires_at.tzinfo else \
            order.expires_at.replace(tzinfo=timezone.utc)
        menit = round((batas - datetime.now(timezone.utc)).total_seconds() / 60)
        kapan = (bahasa.teks("batal_sendiri_menit", lang, menit=menit) if menit >= 1
                 else bahasa.teks("batal_sendiri_segera", lang))
        return bahasa.teks("batal_belum_bisa", lang, label=label, kapan=kapan)

    await store.update_pending_order(order.id, status="cancelled")
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.IDLE)
    # Belum ada uang yang masuk — jangan menjanjikan pengembalian dana.
    return bahasa.teks("batal_belum_bayar", lang)
