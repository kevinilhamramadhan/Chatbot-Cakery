"""Verifikasi nomor WhatsApp untuk pendaftaran Buyer Site.

Arah pembuktiannya terbalik dari OTP biasa: bukan toko yang mengirim kode ke
pelanggan, tapi pelanggan yang mengirim kode ke nomor toko lewat tombol deep
link di Buyer Site. Pesan masuknya sendiri yang jadi bukti — WhatsApp yang
menjamin nomor pengirimnya asli. Konsekuensinya bot tidak pernah mengirim apa
pun ke nomor yang belum menyapa duluan, jadi nomor toko tidak berperilaku
seperti pengirim spam (penting: nomor itu sama dengan nomor chatbot, dan jalan
di atas otomasi tidak resmi).

Chatbot di sini **cuma kurir**: meneruskan {kode, nomor pengirim} ke backend,
lalu membalas sesuai jawabannya. Tidak ada status verifikasi yang disimpan di
sisi kita — satu fakta, satu sumber kebenaran, dan itu ada di backend.
"""

import logging
import re
import time

from app.backend_client import api as backend
from app.core.config import settings
from app.core.security import canonical_wa_number, mask_phone

logger = logging.getLogger(__name__)

# Kode dari Buyer Site: alfanumerik pendek. Rentangnya sengaja longgar supaya
# panjang/alfabet yang dipilih backend tidak memaksa chatbot ikut berubah.
_CODE = r"[A-Za-z0-9]{4,12}"

# Balasan ditulis tangan, tidak lewat LLM: status verifikasi bukan hal yang
# boleh "diimprovisasi" modelnya.
MSG_OK = (
    "Nomor WhatsApp kamu berhasil diverifikasi ✅\n"
    "Silakan kembali ke halaman pendaftaran Toti Cakery untuk melanjutkan.\n\n"
    "Kalau nanti mau pesan lewat sini juga bisa — ketik *menu* buat lihat daftar kue kami 😊"
)
# Sengaja tidak menyebut nomor yang didaftarkan: pengirim yang salah tidak boleh
# jadi tahu nomor pendaftarnya hanya karena dia memegang kodenya.
MSG_MISMATCH = (
    "Nomor ini belum cocok dengan nomor yang didaftarkan ❌\n\n"
    "Kalau kamu punya lebih dari satu WhatsApp, coba kirim ulang pesan tadi dari "
    "nomor yang kamu tulis waktu daftar. Atau ubah nomornya di halaman "
    "pendaftaran, lalu tekan tombol Verifikasi lagi."
)
MSG_NOT_FOUND = (
    "Kode verifikasi ini sudah tidak berlaku ⌛\n"
    "Balik ke halaman pendaftaran, tekan Verifikasi lagi untuk dapat kode baru ya."
)
# Backend membalas 429 setelah 3 kali dikirim dari nomor yang tidak cocok. Kode
# itu sendiri TIDAK dimatikan — pemilik nomor yang benar masih bisa lolos — jadi
# balasannya tidak boleh mengklaim kodenya sudah dinonaktifkan.
MSG_LOCKED = (
    "Sudah 3 kali dikirim dari nomor yang tidak cocok 🔒\n\n"
    "Pastikan mengirim dari nomor yang kamu daftarkan, atau minta kode baru di "
    "halaman pendaftaran."
)
MSG_THROTTLED = (
    "Kamu mencoba verifikasi terlalu sering. Tunggu sebentar ya, lalu minta kode "
    "baru dari halaman pendaftaran."
)
MSG_ERROR = (
    "Maaf, verifikasi sedang tidak bisa diproses. Coba lagi sebentar lagi ya 🙏"
)

_STATUS_REPLY = {
    "ok": MSG_OK,
    "mismatch": MSG_MISMATCH,
    "not_found": MSG_NOT_FOUND,
    "locked": MSG_LOCKED,
}

_WINDOW_SECONDS = 3600
# Cukup di memori: service ini satu proses, dan hilangnya hitungan saat restart
# hanya melonggarkan rem sesaat — backend tetap punya batas percobaannya sendiri.
_attempts: dict[str, list[float]] = {}


def reset_throttle() -> None:
    """Kosongkan hitungan percobaan (dipakai tes)."""
    _attempts.clear()


def _throttled(phone: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(phone, []) if now - t < _WINDOW_SECONDS]
    _attempts[phone] = hits
    return len(hits) >= settings.wa_verification_max_per_hour


def _record(phone: str) -> None:
    _attempts.setdefault(phone, []).append(time.time())


def extract_code(text: str) -> str | None:
    """Ambil kode dari BARIS PERTAMA pesan, atau None kalau bukan pesan verifikasi.

    Hanya baris pertama supaya keterangan tambahan yang ikut di deep link tidak
    mengganggu, dan polanya sengaja ketat (kata kunci + kode) supaya percakapan
    biasa yang kebetulan memuat kata "verifikasi" tetap diteruskan ke LLM.
    """
    if not settings.wa_verification_enabled:
        return None
    first = (text or "").strip().splitlines()
    if not first:
        return None
    keyword = re.escape(settings.wa_verification_keyword)
    m = re.match(rf"^\s*{keyword}[\s:]+({_CODE})\s*$", first[0], re.IGNORECASE)
    return m.group(1).upper() if m else None


async def handle(wa_number: str, code: str) -> str:
    """Teruskan bukti ke backend, kembalikan teks balasan untuk pelanggan."""
    phone = canonical_wa_number(wa_number)
    if not phone:
        logger.warning("Verifikasi dari nomor yang tidak bisa dinormalkan: %s",
                       mask_phone(wa_number))
        return MSG_ERROR

    if _throttled(phone):
        logger.warning("Verifikasi di-rem untuk %s (batas %s/jam)",
                       mask_phone(phone), settings.wa_verification_max_per_hour)
        return MSG_THROTTLED
    _record(phone)

    try:
        result = await backend.confirm_wa_verification(code, phone)
    except Exception:  # noqa: BLE001 — backend mati bukan alasan bot ikut error
        logger.exception("Gagal memanggil backend untuk verifikasi %s",
                         mask_phone(phone))
        return MSG_ERROR

    status = (result or {}).get("status")
    logger.info("Verifikasi %s -> %s", mask_phone(phone), status)
    return _STATUS_REPLY.get(status, MSG_ERROR)
