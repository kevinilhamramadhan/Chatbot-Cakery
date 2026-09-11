"""Tool: sampaikan_maaf — balasan tetap untuk pelanggan yang menyampaikan keluhan.

Kalimatnya sengaja tidak dikarang model. Terukur di uji: "kuenya kemarin basi,
aku kecewa banget" dijawab "Wah, makasih banyak kak! Senang banget kalau suka 😊"
— model 1,7 B salah membaca nada, dan balasan seperti itu jauh lebih merugikan
daripada sekadar salah memanggil tool. Yang tetap jadi keputusan model adalah
KAPAN tool ini dipakai; isinya tidak.

Keluhan tidak menyalakan takeover (itu khusus kue custom), tapi dicatat sebagai
WARNING supaya terlihat saat log ditengok.
"""

import logging

from langchain_core.tools import tool

from app.conversation.context import get_turn_context
from app.core.config import settings
from app.core.security import mask_phone, sanitize_relay

logger = logging.getLogger(__name__)

_TEKS = (
    "Mohon maaf sekali atas ketidaknyamanannya 🙏 Ini jelas bukan pengalaman "
    f"yang kami harapkan dari {settings.store_name}.\n\n"
    "Boleh dibantu ceritakan sedikit lagi — *nomor pesanan* (kalau ada), "
    "*kapan* kuenya diterima, dan kalau memungkinkan *fotonya*? Keluhanmu akan "
    "kami catat dan tindak lanjuti.\n\n"
    "Terima kasih sudah memberi tahu kami ya."
)


@tool
async def sampaikan_maaf(keluhan: str) -> str:
    """Balas pelanggan yang menyampaikan KELUHAN atau kekecewaan.

    Gunakan saat pelanggan mengeluh soal kue, pesanan, atau layanan — misalnya
    kue basi, pesanan telat, salah kirim, atau rasanya tidak sesuai. `keluhan`
    berisi ringkasan singkat keluhannya.

    JANGAN dipakai untuk pertanyaan biasa, dan jangan membalas keluhan dengan
    kalimatmu sendiri — pakai tool ini supaya permintaan maafnya tepat.
    """
    ctx = get_turn_context()
    logger.warning(
        "KELUHAN dari %s: %s", mask_phone(ctx.wa_number), sanitize_relay(keluhan or "")[:200]
    )
    return _TEKS
