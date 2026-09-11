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

    # Pesanan yang sudah dibayar tidak dibatalkan diam-diam: uangnya berpindah,
    # jadi pelanggan harus menjawab "ya" dulu. Pertanyaannya tertutup dan bot
    # sendiri yang mengajukannya, sehingga jawabannya boleh dibaca kode.
    if order.status in ("paid", "ready"):
        if not await _masih_boleh_refund(wa):
            # Kebijakan toko: refund mandiri hanya selama pesanan masih pending.
            # Begitu admin memindahkannya ke "sedang diproses", kuenya sudah
            # dikerjakan. Lebih baik dikatakan sekarang daripada menawarkan
            # pembatalan yang pasti ditolak backend beberapa detik kemudian.
            return (
                "Pesanan ini sudah mulai kami proses, jadi pembatalannya tidak "
                "bisa otomatis lewat chat." + _jalur_tindak_lanjut()
            )
        get_turn_context().next_state = State.AWAITING_CANCEL_CONFIRMATION
        return konfirmasi_batal(order)

    try:
        await backend.cancel_order(order.order_ref)  # order_ref = backend order id
    except Exception as exc:  # noqa: BLE001 - backend menolak 409 kalau sudah dibayar
        logger.warning("cancel failed for %s: %s", order.order_ref, exc)
        # 409 berarti pembayarannya sudah masuk walau catatan lokal belum sempat
        # diperbarui — perlakukan sama: tanya dulu, jangan tutup percakapan.
        get_turn_context().next_state = State.AWAITING_CANCEL_CONFIRMATION
        return konfirmasi_batal(order)

    await store.update_pending_order(order.id, status="cancelled")
    await store.set_cart(wa, [])
    await store.set_state(wa, State.IDLE)
    return "Pesanan kamu sudah dibatalkan. Terima kasih 🙏"


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


def konfirmasi_batal(order) -> str:
    """Pertanyaan tertutup sebelum uang pelanggan diputuskan kembali."""
    label = order.nomor_invoice or f"#{order.order_ref}"
    return (
        f"Pesanan *{label}* sudah dibayar, jadi pembatalannya sekalian dengan "
        "pengembalian dana.\n\n"
        "Mau aku proses sekarang? Ketik *ya* untuk membatalkan dan mengembalikan "
        "dananya, atau *tidak* kalau pesanannya diteruskan saja 🙏"
    )


async def proses_batal_berbayar(wa_number: str) -> str:
    """Jalankan pembatalan + refund sesudah pelanggan menjawab ya.

    Dua jalur dicoba karena backend boleh memilih bentuknya: endpoint cancel
    yang sudah ada (kalau nanti diizinkan menangani pesanan berbayar), lalu
    endpoint refund. Selama keduanya menolak, pelanggan tidak dibiarkan buntu —
    dia diberi alamat email yang benar-benar ditangani orang.
    """
    order = await store.get_active_pending(wa_number)
    if order is None:
        await store.set_state(wa_number, State.IDLE)
        return "Tidak ada pesanan aktif yang perlu dibatalkan 😊"

    berhasil = False
    try:
        await backend.cancel_order(order.order_ref)
        berhasil = True
    except Exception:  # noqa: BLE001 - 409 selama backend belum mengizinkan
        hasil = await backend.refund_order(
            order.order_ref, "Dibatalkan pelanggan lewat chatbot", wa_number)
        berhasil = hasil is not None

    if not berhasil:
        logger.info("refund pelanggan belum bisa otomatis untuk %s", order.order_ref)
        return (
            "Pembatalannya perlu diproses tim kami dulu supaya dananya bisa "
            "dikembalikan." + _jalur_tindak_lanjut()
        )

    await store.update_pending_order(order.id, status="cancelled")
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.IDLE)
    return (
        "Pesanan kamu sudah dibatalkan dan pengembalian dananya diproses ✅\n\n"
        "Dananya kembali lewat metode pembayaran yang kamu pakai, dan bisa makan "
        "beberapa hari kerja tergantung bank atau e-wallet-nya ya 🙏"
    )
