"""Tool: send_apology — balasan tetap untuk pelanggan yang menyampaikan keluhan.

Kalimatnya sengaja tidak dikarang model. Terukur di uji: "kuenya kemarin basi,
aku kecewa banget" dijawab "Wah, makasih banyak kak! Senang banget kalau suka 😊"
— model 1,7 B salah membaca nada, dan balasan seperti itu jauh lebih merugikan
daripada sekadar salah memanggil tool. Yang tetap jadi keputusan model adalah
KAPAN tool ini dipakai; isinya tidak.

Keluhan dijawab permintaan maaf lalu pelanggan DITANYA apakah mau disambungkan
ke admin; takeover baru menyala setelah pelanggan menjawab "ya" (orchestrator,
lewat pending_escalation). Keputusan Kevin (5 Nov 2026), menggantikan alur
27 Sep yang langsung menyalakan takeover dan menyebut email toko. Keluhannya
juga dicatat sebagai WARNING supaya terlihat saat log ditengok.
"""

import logging

from langchain_core.tools import tool

from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.conversation.states import text_is_praise
from app.core.security import mask_phone, sanitize_relay

logger = logging.getLogger(__name__)


def _teks(lang: str) -> str:
    """Permintaan maaf yang tetap, lalu tawaran sambungan ke admin.

    Pelanggan sengaja TIDAK diminta menceritakan ulang kejadiannya di chat:
    yang dia butuhkan saat mengeluh adalah permintaan maaf dan satu jalan tindak
    lanjut yang jelas, bukan formulir.
    """
    return (bahasa.teks("maaf_keluhan", lang) + "\n\n"
            + bahasa.teks("tawaran_admin_keluhan", lang))


@tool
async def send_apology(keluhan: str) -> str:
    """Balas pelanggan yang menyampaikan KELUHAN atau kekecewaan.

    Gunakan saat pelanggan mengeluh soal kue, pesanan, atau layanan — misalnya
    kue basi, pesanan telat, salah kirim, atau rasanya tidak sesuai. `keluhan`
    berisi ringkasan singkat keluhannya.

    JANGAN dipakai untuk pertanyaan biasa, dan jangan membalas keluhan dengan
    kalimatmu sendiri — pakai tool ini supaya permintaan maafnya tepat.
    """
    ctx = get_turn_context()
    # KAPAN tool ini dipakai memang keputusan model, tapi pujian yang dibacanya
    # sebagai keluhan terlalu mahal untuk dibiarkan: admin bisa dikabari
    # "keluhan" yang tidak pernah ada.
    if text_is_praise(ctx.user_text or ""):
        return bahasa.teks("terima_kasih_pujian", await store.get_lang(ctx.wa_number))
    ringkas = sanitize_relay(keluhan or "")[:200]
    logger.warning("KELUHAN dari %s: %s", mask_phone(ctx.wa_number), ringkas)

    # Tawarannya disimpan seperti tawaran escalate_to_admin: kalau pelanggan
    # menjawab "ya", orchestrator menyalakan takeover dan admin menerima
    # ringkasan keluhan ini; "ga usah" membatalkannya.
    await store.set_pending_escalation(ctx.wa_number, f"Keluhan pelanggan: {ringkas}")
    return _teks(await store.get_lang(ctx.wa_number))
