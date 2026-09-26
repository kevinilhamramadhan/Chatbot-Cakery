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
