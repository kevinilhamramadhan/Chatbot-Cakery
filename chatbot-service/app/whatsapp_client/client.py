"""Thin wrapper around the wwebjs-api REST API for sending WhatsApp messages.

We only configure/consume wwebjs-api here — we never reimplement it (PROMPT §13).
"""

import logging

import httpx

from app.core.config import settings
from app.core.security import mask_phone

logger = logging.getLogger(__name__)


def to_chat_id(wa_number: str) -> str:
    """Normalize a bare phone number into a wweb.js chatId (`<digits>@c.us`).

    Pass-through if it already looks like a chatId.
    """
    if "@" in wa_number:
        return wa_number
    digits = "".join(ch for ch in wa_number if ch.isdigit())
    return f"{digits}@c.us"


class WhatsAppClient:
    def __init__(self) -> None:
        self._base = settings.wwebjs_base_url.rstrip("/")
        self._session = settings.wwebjs_session_id
        self._headers = {"x-api-key": settings.wwebjs_api_key}

    async def _post(self, payload: dict) -> dict:
        url = f"{self._base}/client/sendMessage/{self._session}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload, headers=self._headers)
            resp.raise_for_status()
            return resp.json()

    async def send_text(self, wa_number: str, text: str) -> dict:
        payload = {
            "chatId": to_chat_id(wa_number),
            "contentType": "string",
            "content": text,
        }
        if settings.log_message_bodies:
            logger.info("WA out -> %s: %s", mask_phone(wa_number), text[:120])
        else:
            logger.info("WA out -> %s (%d chars)", mask_phone(wa_number), len(text))
        return await self._post(payload)

    async def send_image(
        self, wa_number: str, image_url: str, caption: str | None = None
    ) -> dict:
        """Send an image by URL (used for product photos — PROMPT §10.2)."""
        payload: dict = {
            "chatId": to_chat_id(wa_number),
            "contentType": "MessageMediaFromURL",
            "content": image_url,
        }
        if caption:
            payload["options"] = {"caption": caption}
        logger.info("WA out (image) -> %s: %s", mask_phone(wa_number), image_url)
        return await self._post(payload)


whatsapp_client = WhatsAppClient()
