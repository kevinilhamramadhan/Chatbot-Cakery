"""Tool: send_apology — balasan tetap untuk pelanggan yang menyampaikan keluhan.

Kalimatnya sengaja tidak dikarang model. Terukur di uji: "kuenya kemarin basi,
aku kecewa banget" dijawab "Wah, makasih banyak kak! Senang banget kalau suka 😊"
— model 1,7 B salah membaca nada, dan balasan seperti itu jauh lebih merugikan
daripada sekadar salah memanggil tool. Yang tetap jadi keputusan model adalah
KAPAN tool ini dipakai; isinya tidak.

Keluhan LANGSUNG menyalakan takeover: admin dikabari lewat WhatsApp dan bot
berhenti membalas pelanggan itu sampai admin mengetik "selesai <nomor>".
Keputusan Kevin (27 Sep 2026), menggantikan alur lama yang hanya menawarkan dan
menunggu pelanggan menjawab "ya" — orang yang sedang kecewa tidak perlu diminta
mengiyakan dulu sebelum dilayani manusia. Keluhannya juga dicatat sebagai
WARNING supaya terlihat saat log ditengok.
"""

import logging

from langchain_core.tools import tool

from app.conversation import bahasa, escalation, store
from app.conversation.context import get_turn_context
from app.core.config import settings
from app.core.security import mask_phone, sanitize_relay

logger = logging.getLogger(__name__)

def _teks(lang: str) -> str:
    """Permintaan maaf yang tetap; alamat emailnya diambil dari setelan.

    Pelanggan sengaja TIDAK diminta menceritakan ulang kejadiannya di chat:
    yang dia butuhkan saat mengeluh adalah permintaan maaf dan satu jalan tindak
    lanjut yang jelas, bukan formulir. Kalau STORE_SUPPORT_EMAIL belum diisi,
    kalimat emailnya dilewati — lebih baik tidak menyebut alamat sama sekali
    daripada mengirim pelanggan ke alamat yang tidak ada.
    """
    baris = [bahasa.teks("maaf_keluhan", lang)]
    email = settings.store_support_email.strip()
    if email:
        baris.append(bahasa.teks("maaf_keluhan_email", lang, email=email))
    baris.append(bahasa.teks("maaf_keluhan_penutup", lang))
    return "\n\n".join(baris)


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
    ringkas = sanitize_relay(keluhan or "")[:200]
    logger.warning("KELUHAN dari %s: %s", mask_phone(ctx.wa_number), ringkas)

    lang = await store.get_lang(ctx.wa_number)

    # Permintaan maafnya tetap yang utama, lalu pelanggan LANGSUNG disambungkan
    # ke admin. start_takeover mengabari admin dulu dan baru membungkam bot;
    # kalau tidak ada admin yang bisa dihubungi, ia TIDAK membungkam apa pun dan
    # mengembalikan kalimat "admin tidak tersedia" — pelanggan tidak pernah
    # dijanjikan manusia yang tidak pernah diberi tahu.
    return _teks(lang) + "\n\n" + await escalation.start_takeover(
        ctx.wa_number, f"Keluhan pelanggan: {ringkas}", lang)
