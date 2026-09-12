"""Tool: cancel_order — cancels a still-unpaid order on the backend (B4)."""

import json
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
    """Batalkan pesanan pelanggan.

    Gunakan saat pelanggan minta membatalkan pesanannya. Pesanan yang belum
    dibayar langsung dibatalkan; yang sudah dibayar (DP maupun lunas) dijawab
    dengan pertanyaan konfirmasi lebih dulu, karena pembatalannya sekalian
    dengan pengembalian dana.
    """
    wa = get_turn_context().wa_number
    order = await store.get_active_pending(wa)

    if order is None:
        # Only a draft cart, not a finalized order.
        await store.set_cart(wa, [])
        await store.set_state(wa, State.IDLE)
        return "Oke, draft pesanan dikosongkan. Ada lagi yang bisa kubantu?"

    # SETIAP pembatalan pesanan ditanyakan dulu — bukan hanya yang sudah dibayar.
    # Pesanan sudah masuk ke backend dan ke Admin Site, jadi menghapusnya karena
    # satu kalimat yang bisa saja salah baca terlalu mahal. Draft keranjang yang
    # belum jadi pesanan tetap langsung dikosongkan (lihat di atas): tidak ada
    # yang hilang di sisi toko.
    sudah_dibayar = order.status in ("paid", "ready")
    if sudah_dibayar and not await _masih_boleh_refund(wa):
        # Kebijakan toko: begitu admin memindahkan pesanan ke "sedang diproses",
        # kuenya sudah dikerjakan dan pembatalan mandiri tidak berlaku lagi.
        return _teks_sudah_dikerjakan()

    get_turn_context().next_state = State.AWAITING_CANCEL_CONFIRMATION
    return konfirmasi_batal(order, sudah_dibayar)


def _teks_sudah_dikerjakan() -> str:
    email = settings.store_support_email.strip()
    alamat = email or "kontak resmi Toti Cakery"
    return (
        "Pesanan ini sudah dalam tahap pengerjaan sehingga tidak bisa dibatalkan.\n"
        f"Jika ada keluhan mohon kirim ke alamat email kami di {alamat}\n"
        "Terima Kasih"
    )


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


def konfirmasi_batal(order, sudah_dibayar: bool) -> str:
    """Pertanyaan tertutup sebelum pesanan benar-benar dibatalkan."""
    label = order.nomor_invoice or f"#{order.order_ref}"
    if sudah_dibayar:
        cara = ("Dananya dikembalikan tim kami lewat transfer, jadi butuh waktu "
                "beberapa hari kerja." if _bayar_pakai_va(order) else
                "Dananya kembali otomatis ke aplikasi yang kamu pakai buat bayar.")
        return (
            f"Pesanan *{label}* sudah dibayar, jadi pembatalannya sekalian dengan "
            f"pengembalian dana. {cara}\n\n"
            "Mau aku proses sekarang? Ketik *ya* untuk membatalkan dan mengembalikan "
            "dananya, atau *tidak* kalau pesanannya diteruskan saja 🙏"
        )
    return (
        f"Pesanan *{label}* mau dibatalkan ya?\n\n"
        "Ketik *ya* untuk membatalkan, atau *tidak* kalau pesanannya diteruskan 🙏"
    )


async def proses_pembatalan(wa_number: str) -> str:
    """Jalankan pembatalan sesudah pelanggan menjawab ya.

    Dua jalur dicoba karena backend boleh memilih bentuknya: endpoint cancel
    yang sudah ada (kalau nanti diizinkan menangani pesanan berbayar), lalu
    endpoint refund. Selama keduanya menolak, pelanggan tidak dibiarkan buntu —
    dia diberi alamat email yang benar-benar ditangani orang.
    """
    order = await store.get_active_pending(wa_number)
    if order is None:
        await store.set_state(wa_number, State.IDLE)
        return "Tidak ada pesanan aktif yang perlu dibatalkan 😊"

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
        return (
            "Pembatalannya perlu diproses tim kami dulu supaya dananya bisa "
            "dikembalikan." + _jalur_tindak_lanjut()
        )

    await store.update_pending_order(order.id, status="cancelled")
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.IDLE)
    if not sudah_dibayar:
        # Belum ada uang yang masuk — jangan menjanjikan pengembalian dana.
        return "Pesanan kamu sudah dibatalkan. Terima kasih 🙏"
    if mode == "manual" or (not mode and _bayar_pakai_va(order)):
        email = settings.store_support_email.strip()
        tutup = (f"Kalau dalam 3 hari kerja belum masuk, kabari kami di {email} ya 🙏"
                 if email else "Kalau dalam 3 hari kerja belum masuk, kabari kami ya 🙏")
        return (
            "Pesanan kamu sudah dibatalkan ✅\n\n"
            "Pengembalian dananya diproses tim kami lewat transfer, jadi mohon "
            f"ditunggu beberapa hari kerja. {tutup}"
        )
    return (
        "Pesanan kamu sudah dibatalkan dan pengembalian dananya diproses ✅\n\n"
        "Dananya kembali otomatis ke aplikasi yang kamu pakai buat bayar, biasanya "
        "dalam beberapa menit sampai beberapa jam ya 🙏"
    )
