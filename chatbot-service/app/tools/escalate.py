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


@tool
async def escalate_to_admin(reason: str) -> str:
    """Tawarkan menyambungkan pelanggan ke admin manusia (human takeover).

    Gunakan untuk kue custom atau permintaan di luar kemampuanmu. `reason`
    berisi ringkasan singkat kebutuhan pelanggan. Tool ini hanya MENAWARKAN —
    penyambungan baru terjadi setelah pelanggan menjawab "ya".
    """
    wa = get_turn_context().wa_number
    await store.set_pending_escalation(wa, reason)
    logger.info("Escalation offered to %s", mask_phone(wa))
    return escalation.OFFER_TEXT
