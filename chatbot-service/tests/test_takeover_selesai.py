"""Admin menyudahi takeover lewat WhatsApp: "selesai <nomor>"."""

import pytest

from app.backend_client import api as backend
from app.conversation import escalation, rbac, store

ADMIN_WA = "6281283838610"
PELANGGAN_WA = "628111222333"

DIREKTORI = [
    {"nomor_wa": ADMIN_WA, "role": "Admin", "level": 2, "handles_takeover": True},
]


@pytest.fixture
def direktori(monkeypatch):
    async def f():
        return DIREKTORI

    async def set_takeover(nomor, active, expires_at):
        return {}

    monkeypatch.setattr(backend, "get_role_directory", f)
    monkeypatch.setattr(backend, "set_takeover", set_takeover)
    rbac.bersihkan_cache()


@pytest.mark.asyncio
async def test_admin_mengakhiri_takeover(direktori):
    await store.activate_takeover(PELANGGAN_WA)
    assert await store.is_takeover_active(PELANGGAN_WA)

    # Ditulis seperti orang mengetik di HP: awalan 0 dan ada spasi.
    balasan = await escalation.perintah_selesai(ADMIN_WA, "selesai 0811 1222 333")

    assert balasan and PELANGGAN_WA in balasan
    assert not await store.is_takeover_active(PELANGGAN_WA)


@pytest.mark.asyncio
async def test_pelanggan_tidak_bisa_mengakhiri_takeover_orang_lain(direktori):
    await store.activate_takeover(PELANGGAN_WA)

    balasan = await escalation.perintah_selesai("628999888777",
                                                f"selesai {PELANGGAN_WA}")

    # None = bukan perintah, pesannya diteruskan ke alur biasa.
    assert balasan is None
    assert await store.is_takeover_active(PELANGGAN_WA)


@pytest.mark.asyncio
async def test_pesan_biasa_tidak_dianggap_perintah(direktori):
    assert await escalation.perintah_selesai(ADMIN_WA, "pesanan sudah selesai?") is None
    assert await escalation.perintah_selesai(ADMIN_WA, "selesai") is None


@pytest.mark.asyncio
async def test_sesi_berkunci_c_us_ikut_dilepas(direktori):
    """Sesi dari webhook berkunci "62…@c.us"; admin dulu dijawab "memang tidak
    sedang ditangani" padahal takeover-nya menyala."""
    await store.activate_takeover(f"{PELANGGAN_WA}@c.us")

    balasan = await escalation.perintah_selesai(ADMIN_WA, f"selesai {PELANGGAN_WA}")

    assert "dilepas" in balasan
    assert not await store.is_takeover_active(f"{PELANGGAN_WA}@c.us")


# ── Aturan nomor internal (14 Okt 2026) ───────────────────────────────────────
OWNER_WA = "628777000111"
ADMIN_KEDUA = "628777000333"


def _direktori(monkeypatch, baris):
    async def f():
        return baris

    async def set_takeover(nomor, active, expires_at):
        return {}

    async def upsert(*a, **k):
        return {"customer_id": 1}

    monkeypatch.setattr(backend, "get_role_directory", f)
    monkeypatch.setattr(backend, "set_takeover", set_takeover)
    monkeypatch.setattr(backend, "upsert_customer", upsert)
    rbac.bersihkan_cache()


@pytest.mark.asyncio
async def test_selesai_hanya_untuk_admin_takeover(monkeypatch):
    _direktori(monkeypatch, DIREKTORI + [
        {"nomor_wa": OWNER_WA, "role": "Owner", "level": 1, "handles_takeover": False}])
    await store.activate_takeover(PELANGGAN_WA)

    balasan = await escalation.perintah_selesai(OWNER_WA, f"selesai {PELANGGAN_WA}")
    assert "hanya bisa dipakai admin" in balasan
    assert await store.is_takeover_active(PELANGGAN_WA)


@pytest.mark.asyncio
async def test_nomor_internal_tidak_bisa_memesan(monkeypatch):
    from app.conversation.context import TurnContext, set_turn_context
    from app.tools.add_to_cart import add_to_cart

    _direktori(monkeypatch, [
        {"nomor_wa": OWNER_WA, "role": "Owner", "level": 1, "handles_takeover": False}])
    set_turn_context(TurnContext(wa_number=f"{OWNER_WA}@c.us", user_text="pesan brownies 2"))
    out = await add_to_cart.ainvoke({"items": [{"product": "Brownies Coklat", "qty": 2}]})
    assert "nomor internal" in out and await store.get_cart(f"{OWNER_WA}@c.us") == []


@pytest.mark.asyncio
async def test_takeover_digilir_antar_admin(monkeypatch):
    from app.whatsapp_client.client import whatsapp_client

    _direktori(monkeypatch, DIREKTORI + [
        {"nomor_wa": ADMIN_KEDUA, "role": "Admin", "level": 2, "handles_takeover": True}])
    terkirim = []

    async def kirim(wa, text):
        terkirim.append(wa)
        return {"ok": True}

    monkeypatch.setattr(whatsapp_client, "send_text", kirim)
    monkeypatch.setattr(escalation, "_putaran", 0)
    await escalation.start_takeover("628111000001@c.us", "uji", "id")
    await escalation.start_takeover("628111000002@c.us", "uji", "id")
    # Tiap takeover hanya mengabari satu admin, dan adminnya bergantian.
    assert terkirim == [ADMIN_WA, ADMIN_KEDUA]
