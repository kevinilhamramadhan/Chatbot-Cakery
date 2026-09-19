"""Endpoint internal untuk halaman Admin Site yang mengatur nomor WhatsApp chatbot."""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.main import app
from app.whatsapp_client.client import whatsapp_client

KEY = {"X-Internal-Key": settings.internal_api_key}


@pytest.fixture
def client():
    return TestClient(app)


def _gateway(monkeypatch, state, qr=None):
    async def session_state():
        return state

    async def qr_png():
        return qr

    async def akun():
        return {"nomor": "6287881273160", "nama_profil": "Toti Cakery"}

    monkeypatch.setattr(whatsapp_client, "session_state", session_state)
    monkeypatch.setattr(whatsapp_client, "qr_png", qr_png)
    monkeypatch.setattr(whatsapp_client, "akun", akun)


@pytest.mark.parametrize("method,path", [
    ("get", "/webhook/internal/wa/status"),
    ("get", "/webhook/internal/wa/qr"),
    ("post", "/webhook/internal/wa/ganti-nomor"),
])
def test_butuh_kunci(client, method, path):
    assert getattr(client, method)(path).status_code == 404
    assert getattr(client, method)(path, headers={"X-Internal-Key": "salah"}).status_code == 404


def test_status_tiga_keadaan(client, monkeypatch):
    _gateway(monkeypatch, "CONNECTED")
    r = client.get("/webhook/internal/wa/status", headers=KEY).json()
    assert r == {"keadaan": "tersambung", "nomor": "6287881273160", "nama_profil": "Toti Cakery"}

    _gateway(monkeypatch, "session_not_connected", qr=b"\x89PNG")
    assert client.get("/webhook/internal/wa/status", headers=KEY).json()["keadaan"] == "menunggu_scan"
    r = client.get("/webhook/internal/wa/qr", headers=KEY)
    assert r.status_code == 200 and r.content == b"\x89PNG"
    assert r.headers["cache-control"] == "no-store"

    _gateway(monkeypatch, None)
    assert client.get("/webhook/internal/wa/status", headers=KEY).json()["keadaan"] == "terputus"
    assert client.get("/webhook/internal/wa/qr", headers=KEY).status_code == 404


def test_status_gateway_error_jadi_terputus(client, monkeypatch):
    async def meledak():
        raise httpx.ConnectError("mati")

    _gateway(monkeypatch, "x")
    monkeypatch.setattr(whatsapp_client, "qr_png", meledak)
    assert client.get("/webhook/internal/wa/status", headers=KEY).json()["keadaan"] == "terputus"
    assert client.get("/webhook/internal/wa/qr", headers=KEY).status_code == 503


def test_ganti_nomor_putus_lalu_mulai(client, monkeypatch):
    urutan = []

    async def logout():
        urutan.append("logout")

    async def start_session():
        urutan.append("start")
        return True

    monkeypatch.setattr(whatsapp_client, "logout", logout)
    monkeypatch.setattr(whatsapp_client, "start_session", start_session)
    assert client.post("/webhook/internal/wa/ganti-nomor", headers=KEY).json() == {"status": "ok"}
    assert urutan == ["logout", "start"]
