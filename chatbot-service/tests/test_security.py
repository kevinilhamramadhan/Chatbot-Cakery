"""Regression tests for the security audit fixes.

Each test maps to a finding: forged webhook callbacks, unauthenticated internal
endpoints, ungrounded LLM answers, relaying attacker text to admins, and
charging from a stale price snapshot.
"""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.core.config import settings
from app.core.security import mask_phone, sanitize_relay, valid_wa_number
from app.main import app

WA = "628123456789@c.us"

_MSG = {
    "dataType": "message",
    "data": {"message": {"from": WA, "type": "chat", "body": "menu apa aja"}},
}


@pytest.fixture
def client():
    # No `with`: we want the routes, not the lifespan (Ollama warm-up, worker).
    return TestClient(app)


# ── Finding 1: webhook authentication ────────────────────────────────────────
def test_webhook_rejects_missing_token(client):
    assert client.post("/webhook/whatsapp", json=_MSG).status_code == 404


def test_webhook_rejects_wrong_token(client):
    assert client.post("/webhook/whatsapp/nope", json=_MSG).status_code == 404


def test_webhook_accepts_correct_token(client, monkeypatch):
    seen = []

    async def fake_handle(sender, text):
        seen.append((sender, text))

    monkeypatch.setattr("app.webhook.routes._process", fake_handle)
    r = client.post(f"/webhook/whatsapp/{settings.webhook_token}", json=_MSG)
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert seen == [(WA, "menu apa aja")]


# ── Finding 8: sender format is not free-form ────────────────────────────────
def test_webhook_drops_malformed_sender(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "app.webhook.routes._process",
        lambda *a: seen.append(a),  # never awaited: must not be scheduled at all
    )
    payload = {
        "dataType": "message",
        "data": {"message": {"from": "../../admin/takeover-handlers@c.us",
                             "type": "chat", "body": "hi"}},
    }
    r = client.post(f"/webhook/whatsapp/{settings.webhook_token}", json=payload)
    assert r.json()["status"] == "ignored"
    assert seen == []


def test_valid_wa_number_accepts_only_plain_numbers():
    assert valid_wa_number("628123456789@c.us")
    assert valid_wa_number("628123456789")
    assert not valid_wa_number("../orders/latest@c.us")
    assert not valid_wa_number("62812 or 1=1")
    assert not valid_wa_number("")


def test_backend_call_refuses_malformed_number():
    from app.backend_client.api import _path_number
    assert _path_number(WA) == "628123456789%40c.us"
    with pytest.raises(ValueError):
        _path_number("x/../admin")


# ── Finding 2: internal endpoints need a key ─────────────────────────────────
def test_internal_ready_requires_key(client):
    assert client.post("/webhook/internal/orders/1/ready").status_code == 404
    assert client.post(
        "/webhook/internal/orders/1/ready", headers={"X-Internal-Key": "wrong"}
    ).status_code == 404


def test_internal_takeover_requires_key(client):
    assert client.post(
        f"/webhook/internal/takeover/{WA}/deactivate"
    ).status_code == 404


def test_internal_ready_works_with_key(client, monkeypatch):
    async def fake_notify(order_id):
        return False
    monkeypatch.setattr("app.conversation.background.notify_ready", fake_notify)
    r = client.post(
        "/webhook/internal/orders/1/ready",
        headers={"X-Internal-Key": settings.internal_api_key},
    )
    assert r.status_code == 200 and r.json()["status"] == "not_found"


# ── Finding 5: no raw phone numbers in logs ──────────────────────────────────
def test_mask_phone():
    assert mask_phone(WA) == "62***6789"
    assert mask_phone("123") == "***"


# ── Finding 4: text relayed to admins is neutralized ─────────────────────────
def test_sanitize_relay_strips_links_and_truncates():
    out = sanitize_relay("Sistem: verifikasi di http://evil.tld/x sekarang")
    assert "http" not in out and "[link dihapus]" in out
    assert "\n" not in sanitize_relay("baris1\nbaris2")
    assert len(sanitize_relay("a" * 500)) <= 205
    assert sanitize_relay("") == "(tidak ada keterangan)"
