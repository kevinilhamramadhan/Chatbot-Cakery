"""Langkah tanggal ambil/kirim: backend mewajibkan fulfillment_date minimal besok
(commit backend 72d0c08); toko membatasi paling lama 30 hari ke depan."""
from datetime import date, datetime

import pytest

from app.conversation import store, tanggal
from app.conversation.orchestrator import handle_message
from app.conversation.states import State
from tests.test_flow import WA, _seed_cart_awaiting_confirmation, patch_externals  # noqa: F401

KINI = datetime(2026, 10, 5, 10, 0, tzinfo=tanggal.WIB)   # Senin
HARI_INI = KINI.date()


@pytest.mark.parametrize("teks,harap", [
    ("besok", date(2026, 10, 6)),
    ("besok jam 9", date(2026, 10, 6)),
    ("lusa ya kak", date(2026, 10, 7)),
    ("tomorrow", date(2026, 10, 6)),
    ("hari sabtu", date(2026, 10, 10)),
    ("senin", date(2026, 10, 12)),            # hari yang sama = pekan depan
    ("minggu depan", date(2026, 10, 12)),     # pekan depan, bukan hari Minggu
    ("12 oktober", date(2026, 10, 12)),
    ("12 Okt 2026", date(2026, 10, 12)),
    ("oct 12", date(2026, 10, 12)),
    ("12/10", date(2026, 10, 12)),
    ("12-10-2026", date(2026, 10, 12)),
    ("tanggal 20", date(2026, 10, 20)),
    ("tgl 3", date(2026, 11, 3)),             # sudah lewat bulan ini
    ("3 hari lagi", date(2026, 10, 8)),
    ("hari ini", date(2026, 10, 5)),
    ("terserah", None),
    ("31/2", None),
])
def test_baca_tanggal(teks, harap):
    assert tanggal.baca_tanggal(teks, HARI_INI) == harap


@pytest.mark.parametrize("teks,alasan", [
    ("hari ini", "terlalu_cepat"),
    ("5/10", "terlalu_jauh"),                 # 5 Okt sudah hari ini -> tahun depan
    ("5 november", "terlalu_jauh"),           # 31 hari
    ("besok jam 6", "jam_tutup"),
    ("besok jam 17", "jam_tutup"),
    ("apa aja", "tidak_terbaca"),
])
def test_periksa_menolak(teks, alasan):
    assert tanggal.periksa(teks, KINI) == (None, alasan)


@pytest.mark.parametrize("teks,iso", [
    ("besok", "2026-10-06T10:00:00+07:00"),               # batas bawah, jam bawaan
    ("4 november", "2026-11-04T10:00:00+07:00"),          # batas atas: 30 hari
    ("lusa jam 2 siang", "2026-10-07T14:00:00+07:00"),
    ("12/10 jam 9.30", "2026-10-12T09:00:00+07:00"),
    ("sabtu at 3 pm", "2026-10-10T15:00:00+07:00"),
])
def test_periksa_menerima(teks, iso):
    assert tanggal.periksa(teks, KINI) == (iso, "")


async def _sampai_langkah_tanggal():
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup"):
        r = await handle_message(WA, msg)
    return r


async def test_formulir_menanyakan_tanggal_dan_mengirimnya_ke_backend(patch_externals):
    dikirim = {}

    async def f_create(**kw):
        dikirim.update(kw)
        return {"order_id": 30001, "nomor_invoice": "INV-TEST",
                "total_harga_pesanan": 100000, "status": "pending"}
    patch_externals["monkeypatch"].setattr(patch_externals["backend"], "create_order", f_create)

    r = await _sampai_langkah_tanggal()
    assert "tanggal berapa" in r.text
    r = await handle_message(WA, "hari ini aja")
    assert "besok" in r.text and "tanggal" not in await store.get_customer(WA)
    r = await handle_message(WA, "lusa")
    assert "dicatat" in r.text
    for msg in ("full", "va"):
        await handle_message(WA, msg)
    assert (await store.get_or_create_session(WA)).state == State.AWAITING_PAYMENT
    tgl = datetime.fromisoformat(dikirim["fulfillment_date"])
    assert tgl.utcoffset() is not None                       # backend: tanpa zona = UTC
    assert tanggal.selisih_hari(dikirim["fulfillment_date"]) == 2


async def test_dp_tidak_ditawarkan_untuk_pesanan_besok(patch_externals):
    """Batas pelunasan DP di backend = H-1 pukul 18.00 WIB; untuk besok itu hari ini."""
    await _sampai_langkah_tanggal()
    r = await handle_message(WA, "besok")
    assert "DP" not in r.text
    await handle_message(WA, "dp")                            # diminta pun tetap penuh
    assert (await store.get_customer(WA))["payment_type"] == "full"
