"""Real HTTP client to the main backend (built endpoints B1-B5, C1).

Replaces the old mock_backend. All calls send X-Service-Key. Customer/Order use
`id`; we expose it as `customer_id`/`order_id` for the chatbot's call sites.
"""

import logging
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.security import canonical_wa_number, valid_wa_number

logger = logging.getLogger(__name__)

_TIMEOUT = settings.backend_request_timeout_seconds
# Order/payment creation goes through to Midtrans — give those calls headroom.
_WRITE_TIMEOUT = max(30.0, _TIMEOUT)


def _base() -> str:
    return settings.backend_base_url.rstrip("/")


def _headers() -> dict:
    k = settings.backend_service_api_key
    return {"X-Service-Key": k} if k else {}


def _wa_variants(wa_number: str) -> list[str]:
    """Canonical `62…` first, then the raw JID as a compatibility fallback.

    The chatbot used to send WhatsApp\'s own chat id (`628…@c.us`) straight into
    the customers table, so Admin Site displayed a JID where a phone number
    belongs and no plain-number lookup could match. Writes are canonical from
    now on; reads still try the JID second so customers created before this
    change keep their takeover flag and order history until the backend
    migrates those rows.
    """
    variants: list[str] = []
    canonical = canonical_wa_number(wa_number)
    if canonical:
        variants.append(canonical)
    raw = (wa_number or "").strip()
    if raw and raw not in variants:
        variants.append(raw)
    return variants


async def upsert_customer(wa_number: str, nama: str, alamat: str, phone: str) -> dict:
    number = _wa_variants(wa_number)[0]
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/customers",
                         json={"nomor_wa": number, "nama": nama, "alamat": alamat},
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


async def create_payment(order_id, amount, channel: str = "bank_transfer",
                         payment_type: str = "full") -> dict:
    """Charge via backend -> Midtrans.

    channel ('payment_method'): 'bank_transfer' (VA) | 'qris'.
    payment_type: 'full' | 'dp' — the backend recomputes the expected amount
    from the order for that type and rejects a mismatch with 400 (its
    anti-tampering check), so `amount` must be derived the same way.
    """
    async with httpx.AsyncClient(timeout=_WRITE_TIMEOUT) as c:
        r = await c.post(f"{_base()}/payments",
                         json={"order_id": int(order_id),
                               "payment_method": channel,
                               "payment_type": payment_type,
                               "amount": float(amount)},
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
        for number in _wa_variants(wa_number):
            r = await c.get(f"{_base()}/orders/latest", params={"nomor_wa": number},
                            headers=_headers())
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return r.json()
    return None


async def cancel_order(order_id) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/orders/{int(order_id)}/cancel", headers=_headers())
        r.raise_for_status()
        return r.json()


def _path_number(wa_number: str) -> str:
    """Guard before a phone number is interpolated into a backend URL path.

    These requests carry X-Service-Key, so a crafted value must never be able to
    steer them at another backend path.
    """
    if not valid_wa_number(wa_number):
        raise ValueError(f"refusing backend call for malformed wa_number: {wa_number[:32]!r}")
    return quote(wa_number, safe="")


async def get_takeover_status(wa_number: str) -> dict | None:
    """C1 read: {nomor_wa, human_takeover_active, takeover_expires_at, is_expired}.

    None means "the backend has no row for this number" — the caller must treat
    that as unknown, NOT as "takeover is off".
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for number in _wa_variants(wa_number):
            r = await c.get(f"{_base()}/customers/{_path_number(number)}/takeover",
                            headers=_headers())
            if r.status_code == 404:
                continue
            r.raise_for_status()
            return r.json()
    return None


async def set_takeover(wa_number: str, active: bool, expires_at: str | None) -> dict:
    payload = {"active": active, "expires_at": expires_at}
    last: httpx.Response | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        for number in _wa_variants(wa_number):
            r = await c.post(f"{_base()}/customers/{_path_number(number)}/takeover",
                             json=payload, headers=_headers())
            if r.status_code == 404:
                last = r
                continue
            r.raise_for_status()
            return r.json()
    if last is not None:
        last.raise_for_status()
    return {}


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


async def confirm_wa_verification(code: str, phone: str) -> dict:
    """Teruskan bukti verifikasi ke backend: kode ini datang dari nomor ini.

    Chatbot tidak menyimpan apa pun soal verifikasi — backend yang memutuskan.
    Status non-200 di sini bukan error sistem melainkan jawaban yang sah, jadi
    dipetakan jadi hasil biasa supaya pemanggilnya tidak perlu menangkap
    exception untuk alur yang normal.

    Kontrak nyata backend (commit 59a5d11): POST /auth/verify/wa/confirm dengan
    {nonce, sender_phone}, balasan 200 / 409 mismatch / 429 percobaan habis /
    404 nonce tidak dikenal-kedaluwarsa-sudah dipakai.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_base()}/auth/verify/wa/confirm",
                         json={"nonce": code, "sender_phone": phone},
                         headers=_headers())
    if r.status_code == 200:
        return {"status": "ok"}
    if r.status_code == 404:      # kode tidak dikenal / kedaluwarsa / sudah dipakai
        return {"status": "not_found"}
    if r.status_code == 409:      # nomor pengirim != nomor yang didaftarkan
        return {"status": "mismatch"}
    if r.status_code in (423, 429):   # percobaan habis, kode dikunci
        return {"status": "locked"}
    r.raise_for_status()
    return {"status": "unknown"}
