"""Inbound webhook from wwebjs-api + internal control endpoints.

wwebjs-api posts events here (we configure BASE_WEBHOOK_URL to point at
/webhook/whatsapp/<WEBHOOK_TOKEN>). We only act on text `message` events;
everything else is acknowledged and ignored.

Auth model: the gateway cannot attach headers to its callbacks, so the shared
secret is a path segment. Everything after that trusts `sender` ONLY because it
arrived through an authenticated call — the sender field itself is attacker
data (it decides whose orders get read/cancelled), so it is also format-checked
before it reaches the conversation layer or a backend URL.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request

from app.backend_client import api as backend
from app.conversation import background
from app.conversation.orchestrator import handle_message
from app.conversation.store import deactivate_takeover
from app.core.config import settings
from app.core.security import mask_phone, token_matches, valid_wa_number
from app.whatsapp_client.client import whatsapp_client

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_internal_key(key: str | None) -> None:
    if not token_matches(settings.internal_api_key, key):
        logger.warning("Rejected internal call with missing/invalid X-Internal-Key")
        raise HTTPException(status_code=404, detail="Not Found")


def _extract_message(payload: dict) -> tuple[str, str] | None:
    """Pull (sender_chat_id, text) from a wwebjs-api message event, or None."""
    if payload.get("dataType") != "message":
        return None
    data = payload.get("data") or {}
    msg = data.get("message") or data
    if msg.get("fromMe"):
        return None
    sender = msg.get("from") or ""
    if not sender or sender.endswith("@g.us"):  # ignore groups
        return None
    if not valid_wa_number(sender):
        logger.warning("Dropped message with malformed sender id: %r", sender[:64])
        return None
    # Only plain text chats; media/stickers/etc. are skipped for now.
    if msg.get("type") not in (None, "chat", "text"):
        return None
    body = (msg.get("body") or "").strip()
    if not body:
        return None
    return sender, body


async def _process(sender: str, text: str) -> None:
    try:
        reply = await handle_message(sender, text)
        if reply.suppressed:
            return
        if reply.text:
            await whatsapp_client.send_text(sender, reply.text)
        for media in reply.media:
            await whatsapp_client.send_image(sender, media.image_url, media.caption)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error processing message from %s: %s", mask_phone(sender), exc)


async def _handle_callback(request: Request, bg: BackgroundTasks) -> dict:
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - malformed body from the gateway
        return {"status": "ignored"}
    extracted = _extract_message(payload)
    if extracted is None:
        return {"status": "ignored"}
    sender, text = extracted
    if settings.log_message_bodies:
        logger.info("WA in <- %s: %s", mask_phone(sender), text[:120])
    else:
        logger.info("WA in <- %s (%d chars)", mask_phone(sender), len(text))
    # Ack fast; do the LLM work in the background so wwebjs-api doesn't time out.
    bg.add_task(_process, sender, text)
    return {"status": "accepted"}


@router.post("/whatsapp/{token}")
async def whatsapp_webhook(token: str, request: Request, bg: BackgroundTasks):
    if not token_matches(settings.webhook_token, token):
        logger.warning("Rejected webhook callback with invalid token")
        raise HTTPException(status_code=404, detail="Not Found")
    return await _handle_callback(request, bg)


@router.post("/whatsapp")
async def whatsapp_webhook_untokenized():
    """Old, unauthenticated callback path — kept only to fail loudly.

    If this fires, BASE_WEBHOOK_URL still points at the pre-auth URL and every
    inbound message is being dropped; the log line is how you find that out.
    """
    logger.error(
        "Webhook called WITHOUT a token — update BASE_WEBHOOK_URL to "
        "/webhook/whatsapp/$WEBHOOK_TOKEN. Message dropped."
    )
    raise HTTPException(status_code=404, detail="Not Found")


# ── Internal control endpoints (manual testing helpers) ───────────────────────
@router.post("/internal/takeover/{phone}/deactivate")
async def deactivate(phone: str, x_internal_key: str | None = Header(default=None)):
    """Manually end a human-takeover session (PROMPT §12 — temporary)."""
    _require_internal_key(x_internal_key)
    if not valid_wa_number(phone):
        raise HTTPException(status_code=400, detail="invalid phone")
    await deactivate_takeover(phone)
    try:
        await backend.set_takeover(phone, False, None)  # keep backend in sync
    except Exception:  # noqa: BLE001
        pass
    return {"status": "ok", "phone": mask_phone(phone), "human_takeover_active": False}


@router.post("/internal/orders/{order_id}/ready")
async def mark_ready(order_id: int, x_internal_key: str | None = Header(default=None)):
    """Trigger the proactive 'order is ready' message (PROMPT §10.13).

    Stands in for the backend status webhook, which is out of scope here.
    """
    _require_internal_key(x_internal_key)
    ok = await background.notify_ready(order_id)
    return {"status": "ok" if ok else "not_found", "order_id": order_id}
