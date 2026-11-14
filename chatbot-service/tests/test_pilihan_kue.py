"""Kue yang namanya memuat angka ("Cupcakes Cokelat Isi 4") dan jawaban atas "yang mana?".

Tangkapan layar 9 Nov 2026 (produksi, model v12):
  pelanggan: "saya mau Cupcakes Cokelat Isi 4 yaa"
  bot:       "Ada beberapa pilihan untuk 'Cupcakes Cokelat Isi': Isi 4, Isi 6, Isi 9. Sebutkan yang mana ya?"
  pelanggan: "yang isi 4"
  bot:       seluruh menu

Dua kesalahan: (1) model memotong angka dari nama kue dan memakainya sebagai jumlah, padahal
pelanggan sudah menulis nama lengkapnya; (2) jawaban atas pertanyaan bot sendiri diserahkan ke
model, yang membalasnya dengan menu.
"""

import pytest

from app.conversation import store
from app.conversation.context import TurnContext, set_turn_context
from app.conversation.orchestrator import handle_message
from app.conversation.states import State

WA = "628123456789@c.us"

CUPS = [
    {"id": 31, "nama_produk": "Cupcakes Cokelat Isi 4", "harga_jual": 50000, "is_active": True},
    {"id": 32, "nama_produk": "Cupcakes Cokelat Isi 6", "harga_jual": 72000, "is_active": True},
    {"id": 33, "nama_produk": "Cupcakes Cokelat Isi 9", "harga_jual": 105000, "is_active": True},
    {"id": 41, "nama_produk": "Cupcakes Vanilla Isi 4", "harga_jual": 45000, "is_active": True},
    {"id": 51, "nama_produk": "Bolu Pandan", "harga_jual": 75000, "is_active": True},
]


@pytest.fixture(autouse=True)
def katalog(monkeypatch):
    from app.backend_client import api as backend
    from app.backend_client import products as products_api
    from app.whatsapp_client.client import whatsapp_client

    async def daftar(only_active=True, kategori=None):
        return CUPS

    async def satu(pid):
        return next((p for p in CUPS if p["id"] == pid), None)

    async def tak_ada(*a, **k):
        return None

    async def kirim(wa, text):
        return {"ok": True}

    async def alamat():
        return "Jl. Uji No. 1"

    monkeypatch.setattr(products_api, "list_products", daftar)
    monkeypatch.setattr(products_api, "get_product", satu)
    monkeypatch.setattr(backend, "get_latest_order", tak_ada)
    monkeypatch.setattr(backend, "get_takeover_status", tak_ada)
    monkeypatch.setattr(backend, "get_store_address", alamat)
    monkeypatch.setattr(whatsapp_client, "send_text", kirim)
    return monkeypatch


async def _tambah(teks_pelanggan, items):
    from app.tools.add_to_cart import add_to_cart
    set_turn_context(TurnContext(wa_number=WA, user_text=teks_pelanggan))
    return await add_to_cart.ainvoke({"items": items})


async def test_nama_lengkap_berangka_tidak_ditanyakan_lagi():
    """Model mengirim product='Cupcakes Cokelat Isi', qty=4; pelanggan menulis nama lengkap."""
    out = await _tambah("saya mau Cupcakes Cokelat Isi 4 yaa",
                        [{"product": "Cupcakes Cokelat Isi", "qty": 4}])
    cart = await store.get_cart(WA)
    assert [(c["product_id"], c["qty"]) for c in cart] == [(31, 1)], out
    assert "beberapa pilihan" not in out.lower()


async def test_angka_di_nama_kue_bukan_jumlah():
    """Model mengirim nama yang benar tetapi mengulang angkanya sebagai jumlah."""
    await _tambah("mau cupcakes cokelat isi 6 dong",
                  [{"product": "Cupcakes Cokelat Isi 6", "qty": 6}])
    cart = await store.get_cart(WA)
    assert [(c["product_id"], c["qty"]) for c in cart] == [(32, 1)]


async def test_jumlah_yang_ditulis_terpisah_tetap_dipakai():
    await _tambah("mau Cupcakes Cokelat Isi 4 dua ya",
                  [{"product": "Cupcakes Cokelat Isi", "qty": 2}])
    cart = await store.get_cart(WA)
    assert [(c["product_id"], c["qty"]) for c in cart] == [(31, 2)]


async def test_jumlah_model_yang_tidak_ditulis_pelanggan_ditanyakan():
    """Nama lengkap + angka lain di pesan, tetapi jumlah dari model bukan angka itu."""
    out = await _tambah("mau Cupcakes Cokelat Isi 4 tiga ya",
                        [{"product": "Cupcakes Cokelat Isi", "qty": 4}])
    assert await store.get_cart(WA) == []
    assert "jumlah" in out.lower() or "berapa" in out.lower()


async def test_nama_tidak_lengkap_tetap_ditanyakan():
    out = await _tambah("mau 4 cupcakes cokelat", [{"product": "Cupcakes Cokelat", "qty": 4}])
    assert await store.get_cart(WA) == []
    assert "Isi 4" in out and "Isi 6" in out and "Isi 9" in out


async def test_jawaban_yang_mana_tidak_diserahkan_ke_model(katalog):
    import app.conversation.orchestrator as orch

    panggilan = []

    async def model(wa, text):
        panggilan.append(text)
        from app.conversation.orchestrator import Reply
        from app.tools.add_to_cart import add_to_cart
        # Giliran pertama: model memotong nama seperti di produksi, tanpa angka di pesan.
        return Reply(text=await add_to_cart.ainvoke(
            {"items": [{"product": "Cupcakes Cokelat", "qty": 1}]}))

    katalog.setattr(orch, "_run_agent_turn", model)
    r = await handle_message(WA, "saya mau cupcakes cokelat")
    assert "Sebutkan yang mana" in r.text and len(panggilan) == 1

    async def model_tidak_boleh_dipanggil(wa, text):
        raise AssertionError("jawaban atas 'yang mana?' tidak boleh diteruskan ke model")

    katalog.setattr(orch, "_run_agent_turn", model_tidak_boleh_dipanggil)
    r = await handle_message(WA, "yang isi 4")
    cart = await store.get_cart(WA)
    assert [(c["product_id"], c["qty"]) for c in cart] == [(31, 1)], r.text
    assert "Cupcakes Cokelat Isi 4" in r.text
    assert (await store.get_or_create_session(WA)).state == State.AWAITING_CART_CONFIRMATION


@pytest.mark.parametrize("jawaban, id_kue", [("isi 6 aja", 32), ("yang 9 kak", 33),
                                             ("Cupcakes Cokelat Isi 4", 31), ("4", 31)])
async def test_ragam_jawaban_yang_mana(katalog, jawaban, id_kue):
    import app.conversation.orchestrator as orch

    await store.log_message(WA, "in", "mau cupcakes cokelat")
    await store.log_message(
        WA, "out", "Ada beberapa pilihan untuk 'Cupcakes Cokelat': Cupcakes Cokelat Isi 4 (Rp50.000), "
                   "Cupcakes Cokelat Isi 6 (Rp72.000), Cupcakes Cokelat Isi 9 (Rp105.000). "
                   "Sebutkan yang mana ya? 😊")

    async def model_tidak_boleh_dipanggil(wa, text):
        raise AssertionError("tidak boleh diteruskan ke model")

    katalog.setattr(orch, "_run_agent_turn", model_tidak_boleh_dipanggil)
    await handle_message(WA, jawaban)
    cart = await store.get_cart(WA)
    assert [(c["product_id"], c["qty"]) for c in cart] == [(id_kue, 1)]


async def test_jawaban_di_luar_pilihan_tetap_ke_model(katalog):
    """Bukan salah satu pilihan (kue lain, pertanyaan, atau cocok ke dua pilihan): model yang menjawab."""
    import app.conversation.orchestrator as orch
    from app.conversation.orchestrator import Reply

    await store.log_message(WA, "in", "mau cupcakes cokelat")
    await store.log_message(
        WA, "out", "Ada beberapa pilihan untuk 'Cupcakes Cokelat': Cupcakes Cokelat Isi 4 (Rp50.000), "
                   "Cupcakes Cokelat Isi 6 (Rp72.000), Cupcakes Cokelat Isi 9 (Rp105.000). "
                   "Sebutkan yang mana ya? 😊")
    diterima = []

    async def model(wa, text):
        diterima.append(text)
        return Reply(text="ok")

    katalog.setattr(orch, "_run_agent_turn", model)
    for pesan in ("bolu pandan aja deh", "bedanya apa?", "yang cokelat", "yang isi 4 dua box"):
        await handle_message(WA, pesan)
        await store.log_message(
            WA, "out", "Ada beberapa pilihan untuk 'Cupcakes Cokelat': Cupcakes Cokelat Isi 4 (Rp50.000), "
                       "Cupcakes Cokelat Isi 6 (Rp72.000), Cupcakes Cokelat Isi 9 (Rp105.000). "
                       "Sebutkan yang mana ya? 😊")
    assert len(diterima) == 4
    assert await store.get_cart(WA) == []
