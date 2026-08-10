"""HTTP client to the main backend for public product/FAQ reads.

Base URL comes from config (BACKEND_BASE_URL). The backend's old double-prefix
routing bug is fixed upstream, so paths here are the clean, real ones.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class BackendClient:
    def __init__(self) -> None:
        self._base = settings.backend_base_url.rstrip("/")
        self._timeout = settings.backend_request_timeout_seconds
        # Service-to-service auth header (backend's require_service_key).
        self._headers = (
            {"X-Service-Key": settings.backend_service_api_key}
            if settings.backend_service_api_key
            else {}
        )

    async def get(self, path: str, params: dict | None = None) -> httpx.Response | None:
        """GET a backend path; None when unreachable or the response is >= 400."""
        url = f"{self._base}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
                resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:  # connection/timeout
            logger.warning("Backend unreachable at %s: %s", url, exc)
            return None
        if resp.status_code >= 400:
            logger.warning("Backend %s at %s", resp.status_code, url)
            return None
        return resp


backend_client = BackendClient()
