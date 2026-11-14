"""Rate limit backend (HTTP 429): ditahan sampai Retry-After, tidak diulang sendiri.

Backend membatasi permintaan per IP per endpoint dan chatbot hanya punya satu
IP, jadi semua percakapan berbagi satu kuota. Uji di sini memakai httpx
sungguhan dengan MockTransport supaya kait permintaan/balasan ikut teruji.
"""

import httpx
import pytest

from app.backend_client import api as api_mod
from app.backend_client import batas
from app.conversation import verification

LIMITER = {"error": "Rate limit exceeded: 5 per 1 minute"}


@pytest.fixture
def backend_palsu(monkeypatch):
    """Ganti transport httpx di app.backend_client.api; catat tiap permintaan."""
    keadaan = {"panggilan": [], "balas": lambda request: httpx.Response(200, json={})}

    def handler(request):
        keadaan["panggilan"].append(f"{request.method} {request.url.path}")
        return keadaan["balas"](request)

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(api_mod.httpx, "AsyncClient", factory)
    return keadaan


async def test_429_limiter_menahan_endpoint_sampai_retry_after(backend_palsu, monkeypatch):
    backend_palsu["balas"] = lambda r: httpx.Response(
        429, headers={"Retry-After": "30"}, json=LIMITER)

    with pytest.raises(httpx.HTTPStatusError) as pertama:
        await api_mod.get_payment_status(101, "628123456789")
    assert batas.karena_limit(pertama.value)
    assert len(backend_palsu["panggilan"]) == 1

    # Pesanan lain, rute yang sama: tidak boleh menyentuh jaringan lagi.
    with pytest.raises(batas.BackendSibuk) as kedua:
        await api_mod.get_payment_status(202, "628123456789")
    assert len(backend_palsu["panggilan"]) == 1
    assert 1 <= kedua.value.detik <= 30

    # Rute lain tidak ikut ditahan.
    backend_palsu["balas"] = lambda r: httpx.Response(200, json={"id": 7})
    assert await api_mod.get_order(7) == {"id": 7}
    assert len(backend_palsu["panggilan"]) == 2

    # Sesudah Retry-After lewat, rute tadi dipanggil lagi.
    asli = batas.time.monotonic
    monkeypatch.setattr(batas.time, "monotonic", lambda: asli() + 31)
    assert await api_mod.get_payment_status(101, "628123456789") == {"id": 7}
    assert len(backend_palsu["panggilan"]) == 3


async def test_429_tanpa_retry_after_ditahan_satu_jendela(backend_palsu):
    backend_palsu["balas"] = lambda r: httpx.Response(429, json=LIMITER)
    with pytest.raises(httpx.HTTPStatusError):
        await api_mod.get_order(1)
    with pytest.raises(batas.BackendSibuk) as exc:
        await api_mod.get_order(2)
    assert 55 <= exc.value.detik <= 60


async def test_429_bisnis_tidak_menahan_endpoint(backend_palsu):
    """Kode verifikasi yang dikunci (429 + detail) adalah jawaban untuk satu
    pelanggan; pelanggan berikutnya harus tetap sampai ke backend."""
    backend_palsu["balas"] = lambda r: httpx.Response(
        429, json={"detail": "Terlalu banyak percobaan"})
    assert await api_mod.confirm_wa_verification("ABC123", "628111") == {"status": "locked"}

    backend_palsu["balas"] = lambda r: httpx.Response(200, json={})
    assert await api_mod.confirm_wa_verification("DEF456", "628222") == {"status": "ok"}
    assert len(backend_palsu["panggilan"]) == 2


async def test_verifikasi_kena_rate_limit_bukan_kode_dikunci(backend_palsu, monkeypatch):
    monkeypatch.setattr(verification.settings, "wa_verification_enabled", True)
    verification.reset_throttle()
    backend_palsu["balas"] = lambda r: httpx.Response(
        429, headers={"Retry-After": "40"}, json=LIMITER)

    out = await verification.handle("628123456789@c.us", "ABC123")
    assert out == verification.MSG_BUSY

    # Selama ditahan, pelanggan lain juga mendapat pesan ramai, tanpa panggilan baru.
    out = await verification.handle("628777000111@c.us", "XYZ789")
    assert out == verification.MSG_BUSY
    assert len(backend_palsu["panggilan"]) == 1


async def test_get_produk_tidak_diulang_saat_ditahan(monkeypatch):
    from app.backend_client import base as base_mod

    panggilan = []

    def handler(request):
        panggilan.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "20"}, json=LIMITER)

    real_client = httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(base_mod.httpx, "AsyncClient", factory)
    assert await base_mod.backend_client.get("/products/") is None
    assert await base_mod.backend_client.get("/products/") is None
    assert len(panggilan) == 1
