"""Tool: escalate_to_admin — OFFERS a human takeover; it never starts one.

The starting half lives in app/conversation/escalation.py and only runs after
the customer says yes. Splitting it was a QA finding: the model called this tool
on ordinary messages ("pakai nomor ini aja", "dianter aja ke rumah", "ada yang
tanpa telur gak?"), and because takeover silences the bot for days, 29% of the
turns in a live sweep went unanswered — each time with an admin notice whose
"reason" the model had made up.
"""

import logging

from langchain_core.tools import tool

from app.conversation import escalation, store
from app.conversation.context import get_turn_context
from app.core.security import mask_phone

logger = logging.getLogger(__name__)


# v11: permintaan bicara dengan admin/orang tidak lagi dicegat kata kunci di
# orchestrator ("sambungkan ke admin"), jadi tool ini juga melayaninya. Tetap
# hanya MENAWARKAN. Dipakai registry kalau INTENT_DARI_MODEL menyala.
DESKRIPSI_V11 = """Tawarkan menyambungkan pelanggan ke admin. HANYA untuk pesanan kue
custom (desain/rasa/ukuran/tulisan di luar menu) atau pelanggan yang MEMINTA
dilayani admin/orang. Bukan untuk keluhan (`send_apology`) atau pertanyaan yang
belum kamu tahu. `reason`: ringkasan singkat kebutuhannya. Hanya menawarkan."""


@tool
async def escalate_to_admin(reason: str) -> str:
    """Tawarkan menyambungkan pelanggan ke admin, KHUSUS untuk pesanan kue custom.

    Kue custom = desain, rasa, ukuran, atau tulisan yang tidak ada di menu dan
    harus dibicarakan orang, misalnya "kue ulang tahun tema Frozen tiga tingkat".

    JANGAN panggil tool ini untuk hal lain — bukan untuk pertanyaan yang belum
    kamu tahu jawabannya, bukan untuk keluhan, bukan untuk pelanggan yang minta
    bicara dengan orang, dan bukan untuk pesanan biasa dari menu. Kalau kamu
    tidak tahu jawabannya, katakan apa adanya.

    `reason` berisi ringkasan singkat kue custom yang diminta. Tool ini hanya
    MENAWARKAN — penyambungan terjadi setelah pelanggan menjawab "ya".
    """
    wa = get_turn_context().wa_number
    await store.set_pending_escalation(wa, reason)
    logger.info("Escalation offered to %s", mask_phone(wa))
    return escalation.teks_tawaran(await store.get_lang(wa))
