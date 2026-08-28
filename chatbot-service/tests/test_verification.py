"""Verifikasi nomor WhatsApp untuk pendaftaran Buyer Site.

Alurnya terbalik dari OTP biasa: pelanggan yang MENGIRIM kode ke nomor toko,
lewat tombol deep link di Buyer Site. Chatbot cuma kurir — dia meneruskan
{kode, nomor pengirim} ke backend dan membalas sesuai jawabannya.
"""

import pytest

from app.conversation import store
from app.conversation.orchestrator import handle_message
from app.core.security import canonical_wa_number

pytestmark = pytest.mark.asyncio

WA = "628999000111@c.us"


@pytest.fixture
def backend_calls(monkeypatch):
    """Ganti backend + LLM dengan stub; kembalikan daftar panggilan yang terjadi."""
    from app.backend_client import api as backend
    from app.conversation import orchestrator, verification
    from app.core.config import settings

    calls: list[tuple[str, str]] = []
    box = {"status": "ok"}

    async def fake_confirm(code: str, phone: str) -> dict:
        calls.append((code, phone))
        if isinstance(box["status"], Exception):
            raise box["status"]
        return {"status": box["status"]}

    async def fake_agent(*a, **k):  # LLM tidak boleh kepanggil di tes ini
        return orchestrator.Reply(text="__AGENT__")

    monkeypatch.setattr(settings, "wa_verification_enabled", True)
    monkeypatch.setattr(backend, "confirm_wa_verification", fake_confirm)
    monkeypatch.setattr(orchestrator, "_run_agent_turn", fake_agent)
    verification.reset_throttle()
    return {"calls": calls, "box": box}


# ── Normalisasi nomor: paling gampang bikin verifikasi gagal padahal benar ────
@pytest.mark.parametrize("raw,expected", [
    ("0812-3456-7890", "6281234567890"),
    ("+62 812 3456 7890", "6281234567890"),
    ("62 0812 3456 7890", "6281234567890"),
    ("6281234567890@c.us", "6281234567890"),
    ("6281234567890", "6281234567890"),
])
async def test_canonical_number(raw, expected):
    assert canonical_wa_number(raw) == expected


@pytest.mark.parametrize("raw", ["123", "", "abcdef", "1234567890123456789012"])
async def test_canonical_number_rejects_nonsense(raw):
    assert canonical_wa_number(raw) is None


# ── Jalur utama ───────────────────────────────────────────────────────────────
async def test_verification_success(backend_calls):
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert "diverifikasi" in reply.text.lower()
    assert backend_calls["calls"] == [("7KQ3FA", "628999000111")]


async def test_code_is_uppercased_and_extra_lines_ignored(backend_calls):
    await handle_message(WA, "verifikasi 7kq3fa\n\nPesan otomatis dari Toti Cakery")
    assert backend_calls["calls"] == [("7KQ3FA", "628999000111")]


async def test_mismatch_is_rejected_without_leaking_the_registered_number(backend_calls):
    backend_calls["box"]["status"] = "mismatch"
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert "cocok" in reply.text.lower()
    # Balasan tidak boleh membocorkan nomor apa pun ke pengirim yang salah.
    assert not any(ch.isdigit() for ch in reply.text.replace("Toti", ""))


async def test_unknown_or_expired_code(backend_calls):
    backend_calls["box"]["status"] = "not_found"
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert "berlaku" in reply.text.lower()


async def test_locked_after_too_many_wrong_numbers(backend_calls):
    backend_calls["box"]["status"] = "locked"
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert "kode baru" in reply.text.lower()


async def test_backend_down_gets_a_friendly_reply(backend_calls):
    backend_calls["box"]["status"] = RuntimeError("connection refused")
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert reply.text and "__AGENT__" not in reply.text
    assert "coba lagi" in reply.text.lower()


# ── Yang TIDAK boleh ditangkap intercept ─────────────────────────────────────
async def test_normal_message_still_reaches_the_agent(backend_calls):
    reply = await handle_message(WA, "kak mau verifikasi pesanan saya dong")
    assert reply.text == "__AGENT__"
    assert backend_calls["calls"] == []


async def test_keyword_without_code_reaches_the_agent(backend_calls):
    reply = await handle_message(WA, "VERIFIKASI")
    assert reply.text == "__AGENT__"
    assert backend_calls["calls"] == []


# ── Rem percobaan ────────────────────────────────────────────────────────────
async def test_throttled_after_hourly_limit(backend_calls):
    from app.core.config import settings

    for _ in range(settings.wa_verification_max_per_hour):
        await handle_message(WA, "VERIFIKASI 7KQ3FA")
    before = len(backend_calls["calls"])

    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert "terlalu sering" in reply.text.lower()
    assert len(backend_calls["calls"]) == before  # backend tidak dipanggil lagi


# ── Takeover ─────────────────────────────────────────────────────────────────
async def test_verification_still_works_during_human_takeover(backend_calls):
    """Pelanggan sedang menunggu di halaman Buyer Site — verifikasi tidak boleh
    ikut dibungkam hanya karena admin sedang menangani chat ini."""
    await store.activate_takeover(WA)
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert reply.suppressed is False
    assert "diverifikasi" in reply.text.lower()


async def test_disabled_feature_leaves_the_message_to_the_agent(backend_calls, monkeypatch):
    """Default fitur ini mati sampai backend menyediakan endpointnya."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "wa_verification_enabled", False)
    reply = await handle_message(WA, "VERIFIKASI 7KQ3FA")
    assert reply.text == "__AGENT__"
    assert backend_calls["calls"] == []
