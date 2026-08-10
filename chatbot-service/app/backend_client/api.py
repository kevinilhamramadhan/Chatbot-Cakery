"""Real HTTP client to the main backend (built endpoints B1-B5, C1).

Replaces the old mock_backend. All calls send X-Service-Key. Customer/Order use
`id`; we expose it as `customer_id`/`order_id` for the chatbot's call sites.
"""

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = settings.backend_request_timeout_seconds
# Order/payment creation goes through to Midtrans — give those calls headroom.
_WRITE_TIMEOUT = max(30.0, _TIMEOUT)


def _base() -> str:
    return settings.backend_base_url.rstrip("/")


def _headers() -> dict:
    k = settings.backend_service_api_key
    return {"X-Service-Key": k} if k else {}


async def upsert_customer(wa_number: str, nama: str, alamat: str, phone: str) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/customers",
                         json={"nomor_wa": wa_number, "nama": nama, "alamat": alamat},
                         headers=_headers())
        r.raise_for_status()
        d = r.json()
        d["customer_id"] = d.get("id")
        return d


async def create_order(customer_id: int, items: list[dict], metode_pengiriman: str,
                       created_via: str = "chatbot") -> dict:
    payload = {
        "customer_id": customer_id,
        "metode_pengiriman": metode_pengiriman,
        "created_via": created_via,
        "items": [{"product_id": i["product_id"], "jumlah": i.get("jumlah", i.get("qty"))}
                  for i in items],
    }
    async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as c:
        r = await c.post(f"{_base()}/orders", json=payload, headers=_headers())
        r.raise_for_status()
        d = r.json()
        inv = d.get("invoice") or {}
        return {"order_id": d.get("id"), "nomor_invoice": inv.get("nomor_invoice"),
                "total_harga_pesanan": d.get("total_harga_pesanan"), "status": d.get("status")}


async def create_payment(order_id, amount, channel: str = "bank_transfer") -> dict:
    """Charge via backend -> Midtrans. channel: 'bank_transfer' (VA) | 'qris'."""
    async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as c:
        r = await c.post(f"{_base()}/payments",
                         json={"order_id": int(order_id), "payment_type": channel, "amount": float(amount)},
                         headers=_headers())
        r.raise_for_status()
        return r.json()  # {payment_id, pg_transaction_id, va_number, qris_url, status}


async def get_payment_status(order_id) -> dict | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/payments/{int(order_id)}/status", headers=_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()  # {order_id, invoice_status, amount_paid, amount_due, payments[]}


async def get_latest_order(wa_number: str) -> dict | None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/orders/latest", params={"nomor_wa": wa_number}, headers=_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def cancel_order(order_id) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/orders/{int(order_id)}/cancel", headers=_headers())
        r.raise_for_status()
        return r.json()


async def get_takeover_status(wa_number: str) -> dict | None:
    """C1 read: {nomor_wa, human_takeover_active, takeover_expires_at, is_expired}."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_base()}/customers/{wa_number}/takeover", headers=_headers())
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def set_takeover(wa_number: str, active: bool, expires_at: str | None) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/customers/{wa_number}/takeover",
                         json={"active": active, "expires_at": expires_at}, headers=_headers())
        r.raise_for_status()
        return r.json()


async def get_report_summary(start_date: str, end_date: str) -> dict | None:
    """Owner reports (financial + analytics), one endpoint for both tools.

    GET /reports/summary?start_date&end_date (X-Service-Key) ->
      {revenue, expenses, order_count, avg_order_value, top_products[]}
    Returns None while the endpoint isn't built / backend unreachable —
    the tools then tell the Owner the report isn't available yet.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_base()}/reports/summary",
                            params={"start_date": start_date, "end_date": end_date},
                            headers=_headers())
            if r.status_code >= 400:
                return None
            return r.json()
    except httpx.HTTPError:
        return None


async def get_takeover_admin_numbers() -> list[str]:
    # C2: GET /admin/takeover-handlers -> {"numbers": [...]}; [] -> caller falls back to env.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_base()}/admin/takeover-handlers", headers=_headers())
            if r.status_code >= 400:
                return []
            return r.json().get("numbers", [])
    except Exception:  # noqa: BLE001
        return []
