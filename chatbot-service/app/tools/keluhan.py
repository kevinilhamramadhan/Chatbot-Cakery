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

def _teks() -> str:
    """Permintaan maaf yang tetap; alamat emailnya diambil dari setelan.

    Pelanggan sengaja TIDAK diminta menceritakan ulang kejadiannya di chat:
    yang dia butuhkan saat mengeluh adalah permintaan maaf dan satu jalan tindak
    lanjut yang jelas, bukan formulir. Kalau STORE_SUPPORT_EMAIL belum diisi,
    kalimat emailnya dilewati — lebih baik tidak menyebut alamat sama sekali
    daripada mengirim pelanggan ke alamat yang tidak ada.
    """
    baris = [
        "Mohon maaf sekali atas ketidaknyamanan yang dialami. Masukanmu akan "
        "menjadi bahan perbaikan kami ke depannya.",
    ]
    email = settings.store_support_email.strip()
    if email:
        baris.append(
            f"Jika ada keluhan lebih lanjut, anda dapat mengirimkannya ke alamat "
            f"email kami di {email} agar tim kami dapat merespon dengan lebih akurat."
        )
    baris.append("Terima kasih")
    return "\n\n".join(baris)


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
    return _teks()
