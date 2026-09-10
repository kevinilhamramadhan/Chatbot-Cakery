"""RBAC: peran diambil dari backend, dan level yang lebih tinggi mewarisi izin."""

import pytest

from app.backend_client import api as backend
from app.conversation import rbac
from app.conversation.context import TurnContext, set_turn_context
from app.tools import reports

OWNER_WA = "6285702286413"
ADMIN_WA = "6281283838610"
STAF_WA = "628333333333"
PELANGGAN_WA = "628111222333"

DIREKTORI = [
    {"nomor_wa": OWNER_WA, "role": "Owner", "level": 1, "handles_takeover": True},
    {"nomor_wa": ADMIN_WA, "role": "Admin", "level": 2, "handles_takeover": True},
    {"nomor_wa": "0" + STAF_WA[2:], "role": "Staff", "level": 3, "handles_takeover": False},
]


@pytest.fixture
def direktori(monkeypatch):
    async def f():
        return DIREKTORI

    monkeypatch.setattr(backend, "get_staff_directory", f)
    rbac.bersihkan_cache()
    return DIREKTORI


@pytest.mark.asyncio
async def test_level_dibaca_dari_direktori(direktori):
    assert await rbac.level(OWNER_WA) == rbac.OWNER
    assert await rbac.level(ADMIN_WA) == rbac.ADMIN
    assert await rbac.level(STAF_WA) == rbac.STAFF
    assert await rbac.level(PELANGGAN_WA) == rbac.PELANGGAN


@pytest.mark.asyncio
async def test_owner_mewarisi_izin_admin_dan_staf(direktori):
    """Inti RBAC-nya: satu orang tidak perlu didaftarkan di tiap level."""
    assert await rbac.boleh(OWNER_WA, rbac.OWNER)
    assert await rbac.boleh(OWNER_WA, rbac.ADMIN)
    assert await rbac.boleh(OWNER_WA, rbac.STAFF)
    # Sebaliknya tidak berlaku: Admin bukan Owner.
    assert not await rbac.boleh(ADMIN_WA, rbac.OWNER)
    assert await rbac.boleh(ADMIN_WA, rbac.ADMIN)
    # Pelanggan biasa tidak lolos level mana pun.
    assert not await rbac.boleh(PELANGGAN_WA, rbac.STAFF)


@pytest.mark.asyncio
async def test_nomor_lokal_dinormalkan(direktori):
    """`08…` di kolom nomor_wa_admin tidak bisa dijangkau WhatsApp."""
    orang = await rbac.siapa(STAF_WA)
    assert orang is not None and orang.nomor == STAF_WA
    assert await rbac.siapa("0" + STAF_WA[2:]) is not None


@pytest.mark.asyncio
async def test_penerima_takeover_disaring_flag_bukan_peran(direktori):
    penerima = await rbac.nomor_penerima_takeover()
    assert penerima == [OWNER_WA, ADMIN_WA], "Owner yang bertugas harus ikut menerima"
    assert STAF_WA not in penerima


@pytest.mark.asyncio
async def test_tambalan_dipakai_saat_endpoint_belum_ada(monkeypatch):
    """Tanpa endpoint direktori, peran disusun dari takeover-handlers + .env."""

    async def f_direktori():
        return None

    async def f_penerima():
        return ["0" + ADMIN_WA[2:]]

    monkeypatch.setattr(backend, "get_staff_directory", f_direktori)
    monkeypatch.setattr(backend, "get_takeover_admin_numbers", f_penerima)
    rbac.bersihkan_cache()

    # OWNER_WA_NUMBERS di conftest = 628777000222.
    assert await rbac.level("628777000222") == rbac.OWNER
    assert await rbac.level(ADMIN_WA) == rbac.ADMIN
    assert await rbac.nomor_penerima_takeover() == [ADMIN_WA]


@pytest.mark.asyncio
async def test_direktori_gagal_pakai_cache_lama(monkeypatch, direktori):
    """Backend mati tidak boleh mendadak menurunkan Owner jadi pelanggan."""
    assert await rbac.level(OWNER_WA) == rbac.OWNER

    async def f_meledak():
        raise RuntimeError("backend mati")

    monkeypatch.setattr(backend, "get_staff_directory", f_meledak)
    assert await rbac.direktori(paksa_segar=True) != []
    assert await rbac.level(OWNER_WA) == rbac.OWNER


@pytest.mark.asyncio
async def test_laporan_hanya_untuk_owner(monkeypatch, direktori):
    async def f_summary(start, end):
        return {"revenue": 1000, "expenses": 400, "order_count": 3,
                "avg_order_value": 333, "top_products": []}

    monkeypatch.setattr(backend, "get_report_summary", f_summary)

    set_turn_context(TurnContext(wa_number=f"{OWNER_WA}@c.us"))
    assert "Laporan Keuangan" in await reports.financial_report.ainvoke({})
    assert "Analitik Bisnis" in await reports.business_analytics.ainvoke({})

    for nomor in (ADMIN_WA, STAF_WA, PELANGGAN_WA):
        set_turn_context(TurnContext(wa_number=f"{nomor}@c.us"))
        assert "hanya untuk Owner" in await reports.financial_report.ainvoke({})
        assert "hanya untuk Owner" in await reports.business_analytics.ainvoke({})
