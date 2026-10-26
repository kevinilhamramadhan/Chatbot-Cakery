"""v11: intent pelanggan diputuskan model, bukan aturan kata kunci orchestrator."""

import importlib

import pytest

from app.conversation import bahasa, store
from app.conversation.context import TurnContext, set_turn_context
from app.conversation.orchestrator import handle_message
from app.conversation.states import text_perintah_lunasi
from app.core.config import settings
from tests.test_qa_regressions import WA, _mock_agent, patch_externals  # noqa: F401


@pytest.fixture
def v11(monkeypatch):
    monkeypatch.setattr(settings, "intent_dari_model", True)


@pytest.mark.parametrize("pesan", [
    "makasih ya", "enak banget kuenya", "rekomendasi dong", "bisa kurang ga harganya",
    "mau ngobrol sama admin", "tolong pakai bahasa inggris", "sisanya berapa?",
])
async def test_kata_kunci_tidak_lagi_mencegat(patch_externals, monkeypatch, v11, pesan):
    _mock_agent(monkeypatch, answer="(model)")
    r = await handle_message(WA, pesan)
    assert r.text == "(model)", pesan


async def test_v10_tetap_memakai_aturan(patch_externals, monkeypatch):
    _mock_agent(monkeypatch, answer="(model)")
    r = await handle_message(WA, "makasih ya")
    assert r.text == bahasa.teks("sama_sama", bahasa.ID)


def test_perintah_lunasi_hanya_kata_persis():
    assert text_perintah_lunasi("lunasi") and text_perintah_lunasi("Pay balance!")
    assert not text_perintah_lunasi("mau lunasi dong")


async def test_set_language(patch_externals):
    from app.tools.set_language import set_language

    set_turn_context(TurnContext(wa_number=WA))
    out = await set_language.ainvoke({"language": "en"})
    assert await store.get_lang(WA) == bahasa.EN
    assert out == bahasa.teks("bahasa_diganti", bahasa.EN)


def test_registry_v11(monkeypatch):
    from app.tools import registry

    monkeypatch.setattr(settings, "intent_dari_model", True)
    v11 = importlib.reload(registry)
    nama = {t.name for t in v11.TOOLS_UMUM}
    assert {"pay_remaining", "set_language"} <= nama and len(nama) == 14
    menu = next(t for t in v11.TOOLS_UMUM if t.name == "get_menu")
    assert "rekomendasi" in menu.description
    monkeypatch.setattr(settings, "intent_dari_model", False)
    assert len(importlib.reload(registry).TOOLS_UMUM) == 12


async def test_ganti_bahasa_di_langkah_nama_tetap_dikenali(patch_externals, monkeypatch, v11):
    from app.conversation.states import State

    _mock_agent(monkeypatch, answer="(model)")
    await store.get_or_create_session(WA)
    await store.set_state(WA, State.COLLECTING_IDENTITY)
    r = await handle_message(WA, "tolong pakai bahasa inggris")
    assert await store.get_lang(WA) == bahasa.EN
    assert r.text == bahasa.teks("bahasa_diganti", bahasa.EN)


async def test_batal_dengan_keranjang_kosong(patch_externals):
    from app.tools.cancel_order import cancel_order

    set_turn_context(TurnContext(wa_number=WA, user_text="bolu pandannya ga jadi"))
    out = await cancel_order.ainvoke({})
    assert out == bahasa.teks("tidak_jadi_kosong", bahasa.ID)


@pytest.mark.parametrize("pesan", ["sudah sesuai", "Sudah sesuai!", "confirm"])
async def test_sudah_sesuai_dengan_keranjang_kosong(patch_externals, monkeypatch, v11, pesan):
    _mock_agent(monkeypatch, answer="Terima kasih kak, jadi semangat!")
    r = await handle_message(WA, pesan)
    assert r.text == bahasa.teks("keranjang_kosong_tool", bahasa.normalkan(await store.get_lang(WA)))


async def test_sudah_sesuai_dalam_kalimat_tetap_ke_model(patch_externals, monkeypatch, v11):
    _mock_agent(monkeypatch, answer="(model)")
    assert (await handle_message(WA, "kuenya sudah sesuai pesanan, makasih")).text == "(model)"
