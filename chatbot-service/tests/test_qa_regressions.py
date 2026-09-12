"""Regressions from the live QA sweep (3 Sep 2026).

Every test here started as a transcript from a real conversation run through
`handle_message` with the real model, so each one names the customer-visible
failure it locks down rather than the code path it happens to touch.
"""

import datetime as dt
import json

import httpx
import pytest

from app.conversation import background, store
from app.conversation.context import TurnContext, set_turn_context
from app.conversation.orchestrator import handle_message
from app.conversation.states import State
from app.core.config import settings as settings_module

WA = "628123456789@c.us"

FAKE_PRODUCTS = [
    {"id": 5, "nama_produk": "Brownies Coklat", "deskripsi": "Brownies fudgy",
     "kategori": "Brownies", "harga_jual": 50000, "is_active": True, "minimum_order": 1},
    {"id": 8, "nama_produk": "Bolu Pandan", "deskripsi": "Bolu lembut",
     "kategori": "Bolu", "harga_jual": 75000, "is_active": True, "minimum_order": 1},
]


@pytest.fixture(autouse=True)
def patch_externals(monkeypatch):
    from app.backend_client import api as backend
    from app.backend_client import products as products_api
    from app.whatsapp_client.client import whatsapp_client

    async def fake_list(only_active=True, kategori=None):
        return list(FAKE_PRODUCTS)

    async def fake_get(pid):
        return next((p for p in FAKE_PRODUCTS if p["id"] == pid), None)

    monkeypatch.setattr(products_api, "list_products", fake_list)
    monkeypatch.setattr(products_api, "get_product", fake_get)

    sent = []

    async def fake_send_text(wa, text):
        sent.append((wa, text))
        return {"ok": True}

    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)

    # Kept before the stubs replace them: the two HTTP-level tests below need
    # the real implementations back.
    originals = {name: getattr(backend, name)
                 for name in ("get_takeover_status", "get_latest_order", "cancel_order")}

    cancelled = []

    async def f_cancel(order_id):
        cancelled.append(str(order_id))
        return {"status": "success"}

    async def f_takeover_status(wa):
        return {"nomor_wa": wa, "human_takeover_active": True, "is_expired": False}

    async def f_payment_status(order_id):
        return {"invoice_status": "unpaid", "amount_paid": 0, "amount_due": 0}

    async def f_admin():
        # Penerima takeover datang dari user backend yang handles_takeover.
        return ["628999000111"]

    monkeypatch.setattr(backend, "get_takeover_admin_numbers", f_admin)
    monkeypatch.setattr(backend, "cancel_order", f_cancel)
    monkeypatch.setattr(backend, "get_takeover_status", f_takeover_status)
    monkeypatch.setattr(backend, "get_payment_status", f_payment_status)
    return {"sent": sent, "cancelled": cancelled, "backend": backend,
            "monkeypatch": monkeypatch, "originals": originals}


def _mock_agent(monkeypatch, answer="(jawaban agent)"):
    """Replace the LLM turn; these tests are about the deterministic paths."""
    from app.conversation import orchestrator

    async def fake_run_agent(wa, text, history):
        return answer

    monkeypatch.setattr(orchestrator, "run_agent", fake_run_agent)


# ── P0-2 · takeover must survive a backend that doesn't know the number ───────
async def test_takeover_holds_when_backend_has_no_customer_row(patch_externals):
    """Live: bot promised "admin akan menghubungimu", then kept chatting.

    The backend only learns about a customer at checkout, so the most common
    escalation (a new customer asking for a custom cake) 404s — which used to
    read as "admin ended the takeover" and un-muted the bot immediately.
    """
    async def not_found(wa):
        return None
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_takeover_status", not_found)

    await store.activate_takeover(WA)
    reply = await handle_message(WA, "halo?")
    assert reply.suppressed is True
    assert await store.is_takeover_active(WA) is True


async def test_takeover_released_when_backend_says_it_ended(patch_externals):
    async def ended(wa):
        return {"nomor_wa": wa, "human_takeover_active": False, "is_expired": False}
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_takeover_status", ended)
    _mock_agent(patch_externals["monkeypatch"], "Halo! Ada yang bisa kubantu?")

    await store.activate_takeover(WA)
    reply = await handle_message(WA, "halo?")
    assert reply.suppressed is False
    assert await store.is_takeover_active(WA) is False


# ── P0-3 · identity step must not swallow questions as data ──────────────────
async def test_question_at_name_step_is_answered_not_stored(patch_externals):
    """Live: customer #3 in the backend was named
    "siapa aja yang bisa lihat data aku?" with address "rumah"."""
    _mock_agent(patch_externals["monkeypatch"], "Datamu hanya dipakai untuk pesanan ini.")
    await store.set_state(WA, State.COLLECTING_IDENTITY)

    reply = await handle_message(WA, "siapa aja yang bisa lihat data aku?")

    assert "nama" not in await store.get_customer(WA)
    assert "Datamu hanya dipakai" in reply.text
    assert "nama" in reply.text.lower()          # the question is asked again
    assert (await store.get_or_create_session(WA)).state == State.COLLECTING_IDENTITY


async def test_vague_address_is_rejected(patch_externals):
    _mock_agent(patch_externals["monkeypatch"])
    await store.set_customer(WA, {"nama": "Budi"})
    await store.set_state(WA, State.COLLECTING_IDENTITY)

    reply = await handle_message(WA, "rumah")
    assert "alamat" not in await store.get_customer(WA)
    assert "alamat" in reply.text.lower()

    await handle_message(WA, "Jl. Anggrek No. 9 RT 03 Batam")
    assert (await store.get_customer(WA))["alamat"].startswith("Jl. Anggrek")


# ── P0-4 · backend must receive a canonical 62… number ───────────────────────
def _mock_backend_http(monkeypatch, handler):
    """Point app.backend_client.api's httpx clients at a fake transport."""
    from app.backend_client import api as api_mod
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(api_mod.httpx, "AsyncClient", factory)


async def test_customer_is_created_with_canonical_number(patch_externals):
    """Live: the customers table held `628990000001@c.us` next to `628999000111`,
    so Admin Site showed a JID as the phone number and no plain-number lookup
    could ever match."""
    from app.backend_client import api as api_mod
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 1, "nomor_wa": seen["body"]["nomor_wa"]})

    _mock_backend_http(patch_externals["monkeypatch"], handler)
    await api_mod.upsert_customer(WA, "Budi", "Jl. Anggrek 9", "628123456789")
    assert seen["body"]["nomor_wa"] == "628123456789"


async def test_takeover_lookup_falls_back_to_the_raw_jid(patch_externals):
    """Rows written before the fix are keyed by JID; they must still resolve."""
    from app.backend_client import api as api_mod
    tried = []

    def handler(request):
        tried.append(request.url.path)
        if "%40c.us" in str(request.url) or "@c.us" in str(request.url):
            return httpx.Response(200, json={"human_takeover_active": True,
                                             "is_expired": False})
        return httpx.Response(404, json={"detail": "not found"})

    patch_externals["monkeypatch"].setattr(
        api_mod, "get_takeover_status", patch_externals["originals"]["get_takeover_status"])
    _mock_backend_http(patch_externals["monkeypatch"], handler)
    st = await api_mod.get_takeover_status(WA)
    assert st is not None and st["human_takeover_active"] is True
    assert len(tried) == 2                      # canonical first, then the JID


# ── P1-1 · an unmatched category is not a system outage ──────────────────────
async def test_unknown_category_shows_the_full_menu(patch_externals):
    """Live: "ada kue ultah gak?" -> get_menu(kategori="cake") -> 0 rows ->
    "Maaf, daftar menu sedang tidak bisa diambil" — a fake outage message."""
    from app.tools.get_menu import get_menu
    out = await get_menu.ainvoke({"kategori": "cake"})
    assert "tidak bisa diambil" not in out
    assert "Brownies Coklat" in out and "Bolu Pandan" in out


async def test_every_menu_output_is_compressible_in_history(patch_externals):
    """_history_view() keys off the "Berikut menu" prefix; a menu that slips
    back into the LLM context verbatim is what taught the model to answer menu
    questions from memory (insiden #1 of the v4 dataset notes)."""
    from app.llm.agent import _history_view
    from app.tools.get_menu import get_menu
    for kwargs in ({}, {"kategori": "brownies"}, {"kategori": "cake"}):
        out = await get_menu.ainvoke(kwargs)
        assert "get_menu" in _history_view(out), f"not compressed for {kwargs}"


async def test_menu_lists_every_category(patch_externals):
    """Tidak ada lagi penyaringan: pelanggan yang bertanya menu melihat semuanya,
    dikelompokkan supaya tetap enak dibaca."""
    from app.tools.get_menu import get_menu
    out = await get_menu.ainvoke({})
    assert "*Brownies*" in out and "*Bolu*" in out
    assert "Brownies Coklat" in out and "Bolu Pandan" in out


async def test_empty_catalogue_still_reports_an_outage(patch_externals):
    from app.backend_client import products as products_api
    from app.tools.get_menu import get_menu

    async def nothing(only_active=True, kategori=None):
        return []
    patch_externals["monkeypatch"].setattr(products_api, "list_products", nothing)
    assert "tidak bisa diambil" in await get_menu.ainvoke({})


# ── P1-3 · the ungrounded-price guard must not answer with a menu dump ───────
async def test_ungrounded_price_outside_a_menu_question(patch_externals):
    """Live: "udah aku bayar kok" was answered with the full price list."""
    from langchain_core.messages import AIMessage
    from app.llm import agent as agent_mod
    from app.rag.store import RetrievalResult

    class _Bound:
        async def ainvoke(self, messages):
            return AIMessage(content="Oke, pembayaran Rp235.000 sudah tercatat ya.")

    class _LLM:
        def bind_tools(self, tools):
            return _Bound()

    mp = patch_externals["monkeypatch"]
    mp.setattr(agent_mod, "get_llm", lambda: _LLM())
    mp.setattr(agent_mod, "retrieve", lambda q, top_k=None: RetrievalResult([], [], []))

    set_turn_context(TurnContext(wa_number=WA))
    out = await agent_mod.run_agent(WA, "udah aku bayar kok", history=[])
    assert "Rp235.000" not in out            # the invented figure is gone
    assert "Berikut menu" not in out         # and it is not a menu dump either


# ── P1-4 · "sudah saya bayar" is answerable — as a TOOL, not a regex ─────────
async def test_check_payment_status_reports_unpaid(patch_externals):
    """The claim used to be intercepted by a regex in the orchestrator. Routing
    is the model's job, so it is a tool now; this locks the tool's answers."""
    from app.tools.payment_status import check_payment_status
    await _seed_awaiting_payment()
    set_turn_context(TurnContext(wa_number=WA))
    out = await check_payment_status.ainvoke({})
    assert "belum" in out.lower()


async def test_check_payment_status_confirms_when_paid(patch_externals):
    from app.tools.payment_status import check_payment_status

    async def paid(order_id):
        return {"invoice_status": "paid", "amount_paid": 100000, "amount_due": 0}
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", paid)

    await _seed_awaiting_payment()
    ctx = TurnContext(wa_number=WA)
    set_turn_context(ctx)
    out = await check_payment_status.ainvoke({})
    assert "terima" in out.lower()
    assert ctx.next_state == State.ORDER_ACTIVE      # orchestrator applies it


async def test_check_payment_status_without_an_order(patch_externals):
    from app.tools.payment_status import check_payment_status
    set_turn_context(TurnContext(wa_number=WA))
    out = await check_payment_status.ainvoke({})
    assert "tidak menemukan" in out.lower()


async def test_no_static_intent_routing_in_the_orchestrator():
    """Guard rail for a deliberate design rule: the orchestrator may parse the
    answer to a closed question it just asked, and it must honour *batal*, but
    it must never classify what a customer WANTS. That decision belongs to the
    model so it can be improved by fine-tuning."""
    import app.conversation.orchestrator as orch
    src = __import__("pathlib").Path(orch.__file__).read_text()
    for banned in ("_PAID_CLAIM_RE", "_MENU_ASK_RE"):
        assert banned not in src, f"{banned} is static intent routing — use a tool"


async def _seed_awaiting_payment(order_ref="9001"):
    await store.create_pending_order(
        wa_number=WA, order_ref=order_ref, payment_ref="MID", payment_type="full",
        total_amount=100000, amount_due=100000, items_json="[]", customer_json="{}",
        delivery_method="pickup",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )
    await store.set_state(WA, State.AWAITING_PAYMENT)


# ── P2-1 · quantities are never silently rewritten ───────────────────────────
async def test_bulk_quantity_is_confirmed_not_clamped(patch_externals):
    """Live: "brownies panggang 1000 box" silently became 100 -> Rp8.500.000."""
    from app.tools.add_to_cart import add_to_cart
    set_turn_context(TurnContext(wa_number=WA))
    out = await add_to_cart.ainvoke({"items": [{"product": "Brownies Coklat", "qty": 1000}]})
    assert await store.get_cart(WA) == []
    assert "1000" in out


async def test_nonpositive_and_fractional_quantities_ask_again(patch_externals):
    from app.tools.add_to_cart import add_to_cart
    set_turn_context(TurnContext(wa_number=WA))
    for bad in (-5, 0, 0.5):
        out = await add_to_cart.ainvoke({"items": [{"product": "Brownies Coklat", "qty": bad}]})
        assert await store.get_cart(WA) == [], f"qty={bad} should not enter the cart"
        assert "jumlah" in out.lower()


# ── P2-2 · e-wallet names are QRIS ───────────────────────────────────────────
@pytest.mark.parametrize("typed,expected", [
    ("gopay", "qris"), ("OVO", "qris"), ("dana aja", "qris"),
    ("m-banking", "bank_transfer"), ("transfer bank", "bank_transfer"),
])
async def test_channel_understands_wallet_names(patch_externals, typed, expected):
    from app.conversation import checkout
    captured = {}

    async def fake_finalize(wa):
        captured["channel"] = (await store.get_customer(wa)).get("channel")
        return "ok"
    patch_externals["monkeypatch"].setattr(checkout, "finalize_order", fake_finalize)

    await store.set_customer(WA, {"nama": "Budi", "alamat": "Jl. Anggrek No. 9 Batam",
                                  "metode_pengiriman": "pickup", "nomor_hp": "628123456789",
                                  "payment_type": "full"})
    await store.set_state(WA, State.COLLECTING_IDENTITY)
    await handle_message(WA, typed)
    assert captured["channel"] == expected


# ── P2-3 · an expired order is cancelled on the backend too ──────────────────
async def test_expired_order_is_cancelled_upstream(patch_externals):
    """Live gap: the local row went "expired" and the customer was told the
    order was cancelled, while the backend kept it pending forever."""
    await store.create_pending_order(
        wa_number=WA, order_ref="9002", payment_ref="MID", payment_type="full",
        total_amount=100000, amount_due=100000, items_json="[]", customer_json="{}",
        delivery_method="pickup",
        expires_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
    )
    await background._check_once()
    assert patch_externals["cancelled"] == ["9002"]
    assert any("9002" in text for _, text in patch_externals["sent"])


# ── P2-4 · three messages at once must not lose two of them ──────────────────
async def test_concurrent_first_messages_share_one_session(patch_externals):
    """Live: sqlite3.IntegrityError UNIQUE constraint failed: sessions.wa_number
    — two of three messages were dropped without any reply."""
    import asyncio
    rows = await asyncio.gather(*(store.get_or_create_session(WA) for _ in range(5)))
    assert all(r is not None for r in rows)
    assert len({r.wa_number for r in rows}) == 1


# ── Paritas prompt · pesan saat ini tidak boleh muncul di riwayatnya sendiri ─
async def test_current_message_is_not_repeated_in_history(patch_externals):
    """handle_message mencatat pesan masuk SEBELUM merutekan, jadi tanpa
    penyaringan `recent_history` mengembalikannya sebagai entri terakhir dan
    model menerima pesan yang sama dua kali — bentuk yang tidak pernah ada di
    data latih. Terukur pada toti-qwen-1.7b-v5: duplikasi itu sendiri membalik
    "mau order cupcake dong" dari add_to_cart 10/10 ke escalate_to_admin 10/10,
    dan escalate salah = pelanggan dibungkam takeover selama 7 hari."""
    from app.conversation import orchestrator

    seen = {}

    async def spy_run_agent(wa, text, history):
        seen["history"] = list(history)
        return "ok"

    patch_externals["monkeypatch"].setattr(orchestrator, "run_agent", spy_run_agent)

    # Bukan sapaan telanjang: "halo kak" kini dijawab template tanpa model.
    await handle_message(WA, "kuenya apa aja kak")
    assert seen["history"] == [], f"turn pertama harus tanpa riwayat: {seen['history']}"

    await handle_message(WA, "menu dong")
    hist = seen["history"]
    assert all(not (m["role"] == "user" and m["content"] == "menu dong") for m in hist), \
        f"pesan saat ini bocor ke riwayatnya sendiri: {hist}"
    assert hist and hist[0]["content"] == "kuenya apa aja kak"  # giliran sebelumnya tetap ada


# ── P0-1 (partial) · an escalate reply must not teach the model to escalate ──
async def test_history_view_compresses_the_escalate_reply(patch_externals):
    """Live: after one escalate reply landed in the history, "aku mau bento
    cookies 2" routed to escalate_to_admin 3 times out of 3."""
    from app.llm.agent import _history_view
    reply = ("Permintaanmu sudah aku teruskan ke admin kami ya. Mohon tunggu, admin "
             "akan menghubungimu langsung lewat chat ini. 🙏")
    view = _history_view(reply)
    assert "teruskan ke admin kami" not in view
    assert "escalate" in view.lower()


# ══════════════════════════════════════════════════════════════════════════════
#  Regresi dari sapuan QA "di luar alur" (9 Sep 2026)
#  65 giliran, 41 lolos, 19 di antaranya tidak dijawab sama sekali.
# ══════════════════════════════════════════════════════════════════════════════

async def _cart_awaiting_confirmation(items):
    from app.tools.add_to_cart import add_to_cart
    set_turn_context(TurnContext(wa_number=WA))
    await add_to_cart.ainvoke({"items": items})
    await store.set_state(WA, State.AWAITING_CART_CONFIRMATION)


# ── Konfirmasi keranjang ──────────────────────────────────────────────────────
async def test_natural_confirmation_does_not_double_the_cart(patch_externals):
    """Live: "iya udah bener" tidak dikenali sebagai konfirmasi, jatuh ke model,
    dan model memanggil add_to_cart lagi — 2 brownies jadi 4, Rp190.000 jadi
    Rp380.000. Satu-satunya temuan yang langsung berupa uang."""
    _mock_agent(patch_externals["monkeypatch"], "(model tidak boleh dipanggil)")
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])

    reply = await handle_message(WA, "iya udah bener")

    cart = await store.get_cart(WA)
    assert cart[0]["qty"] == 2, f"jumlah berubah tanpa diminta: {cart}"
    assert (await store.get_or_create_session(WA)).state == State.COLLECTING_IDENTITY
    assert "nama" in reply.text.lower()


async def test_unrecognised_message_at_confirmation_asks_again(patch_externals):
    """"AKU MAU PESAN SEKARANG JUGA CEPAT!!!!!" juga menggandakan keranjang.
    Pesan yang tidak dikenali dijawab deterministik, tidak diserahkan ke model."""
    _mock_agent(patch_externals["monkeypatch"], "(model tidak boleh dipanggil)")
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])

    reply = await handle_message(WA, "AKU MAU PESAN SEKARANG JUGA CEPAT!!!!!")

    assert (await store.get_cart(WA))[0]["qty"] == 2
    assert "sudah sesuai" in reply.text.lower() and "batal" in reply.text.lower()
    assert (await store.get_or_create_session(WA)).state == State.AWAITING_CART_CONFIRMATION


async def test_cart_change_at_confirmation_still_reaches_the_model(patch_externals):
    """Penjaga di atas tidak boleh mematikan kemampuan mengubah pesanan."""
    seen = {}

    from app.conversation import orchestrator

    async def spy(wa, text, history):
        seen["text"] = text
        return "(keranjang diubah)"

    patch_externals["monkeypatch"].setattr(orchestrator, "run_agent", spy)
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])

    await handle_message(WA, "eh tambah 1 bolu pandan juga dong")
    assert seen["text"] == "eh tambah 1 bolu pandan juga dong"


# ── Langkah identitas ─────────────────────────────────────────────────────────
async def test_cart_change_at_name_step_is_not_stored_as_the_name(patch_externals):
    """Live: "eh tambahin 1 brownies fudgy almond dong" tersimpan sebagai NAMA
    pelanggan dan dikirim ke backend seperti itu.

    Yang menahannya sekarang bukan penebak maksud berbasis kata kunci, melainkan
    validasi nilainya sendiri: kalimat sepanjang itu bukan nama, jadi giliran ini
    diserahkan ke model dan langkahnya diulang."""
    seen = {}

    from app.conversation import orchestrator

    async def spy(wa, text, history):
        seen["text"] = text
        return "(keranjang diubah)"

    patch_externals["monkeypatch"].setattr(orchestrator, "run_agent", spy)
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    await handle_message(WA, "sudah sesuai")

    reply = await handle_message(WA, "eh tambahin 1 bolu pandan dong")

    cust = json.loads((await store.get_or_create_session(WA)).customer_json)
    assert "nama" not in cust, f"kalimat pesanan tersimpan sebagai nama: {cust}"
    assert seen["text"] == "eh tambahin 1 bolu pandan dong"   # model yang menilai
    assert "nama" in reply.text.lower()                        # langkahnya diulang


async def test_sentence_is_never_accepted_as_a_name(patch_externals):
    _mock_agent(patch_externals["monkeypatch"])
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    await handle_message(WA, "sudah sesuai")

    reply = await handle_message(WA, "aku sebenarnya cuma mau tanya tanya dulu sih kak")

    cust = json.loads((await store.get_or_create_session(WA)).customer_json)
    assert "nama" not in cust
    assert "nama" in reply.text.lower()


# ── Menu ──────────────────────────────────────────────────────────────────────
async def test_menu_always_sends_everything_grouped_by_category(patch_externals):
    """Pelanggan yang bertanya menu ingin melihat semuanya. Model rutin
    menempelkan kategori yang tidak pernah diketik pelanggan, dan begitu katalog
    aslinya masuk (kategori "cake" jadi nyata), "menu dong" polos menjawab 14
    dari 23 produk. Tool ini sekarang tidak punya parameter sama sekali."""
    from app.tools.get_menu import get_menu

    out = await get_menu.ainvoke({})

    assert "Brownies Coklat" in out and "Bolu Pandan" in out
    assert "*Brownies*" in out and "*Bolu*" in out      # dikelompokkan per kategori
    assert out.index("*Bolu*") < out.index("*Brownies*")  # kategori urut abjad


# ── Penjaga keluaran model ────────────────────────────────────────────────────
def test_ungrounded_report_and_tool_names_are_recognised():
    """Live: Owner bertanya "produk apa yang paling laku" dan menerima blok
    "📈 *Analitik Bisnis*" tanpa satu pun tool dipanggil. Bot juga pernah bilang
    "aku bisa panggil tool `get_menu` ya 😊" ke pelanggan."""
    from app.llm.agent import _REPORT_RE, _TOOLNAME_RE

    assert _REPORT_RE.search("📈 *Analitik Bisnis* (2026-09-01 s/d 2026-09-09)")
    assert _REPORT_RE.search("Laporan Keuangan bulan ini")
    assert not _REPORT_RE.search("Pesanan kamu sudah dibuat ✅")
    assert _TOOLNAME_RE.search("aku bisa panggil tool `get_menu` ya 😊")
    assert not _TOOLNAME_RE.search("Ketik *menu* untuk daftar lengkapnya")


# ── Pesan non-teks ────────────────────────────────────────────────────────────
def test_voice_note_gets_a_template_reply_not_silence():
    """Live: voice note, stiker, lokasi, dan foto berketerangan dibuang tanpa
    balasan apa pun — dari sisi pelanggan, toko terlihat mengabaikannya."""
    from app.webhook import routes

    payload = {"dataType": "message",
               "data": {"message": {"from": "628123999888@c.us", "type": "ptt", "body": ""}}}
    assert routes._extract_message(payload) is None          # tetap bukan teks
    assert routes._direct_sender(payload) == "628123999888@c.us"

    routes._text_only_notified.clear()
    assert routes._should_send_text_only_notice("628123999888@c.us") is True
    # Lima stiker beruntun tidak berarti lima balasan yang sama.
    assert routes._should_send_text_only_notice("628123999888@c.us") is False


# ── Checkout ──────────────────────────────────────────────────────────────────
async def test_checkout_refuses_an_invoice_with_no_way_to_pay(patch_externals):
    """Backend menjawab 201 walau Midtrans menolak (406): pg_transaction_id dan
    qris_url sama-sama null. Tagihan tanpa cara bayar tetap dikirim ke pelanggan,
    lengkap dengan tenggat 30 menit di bawahnya."""
    from app.conversation import checkout

    cancelled = []

    async def no_instrument(order_id, amount, channel="bank_transfer", payment_type="full"):
        return {"payment_id": 9, "pg_transaction_id": None,
                "va_number": None, "qris_url": None, "status": "Pending"}

    async def f_cancel(order_id):
        cancelled.append(order_id)
        return {"status": "success"}

    async def f_upsert(wa, nama, alamat, phone):
        return {"id": 1, "customer_id": 1}

    async def f_create_order(customer_id, items, metode_pengiriman, created_via="chatbot"):
        return {"order_id": 30001, "nomor_invoice": "INV-TEST",
                "total_harga_pesanan": 50000, "status": "pending"}

    for name, fn in {"upsert_customer": f_upsert, "create_order": f_create_order,
                     "create_payment": no_instrument, "cancel_order": f_cancel}.items():
        patch_externals["monkeypatch"].setattr(patch_externals["backend"], name, fn)

    await store.set_cart(WA, [{"product_id": 5, "nama": "Brownies Coklat",
                               "harga": 50000.0, "qty": 1}])
    await store.set_customer(WA, {"nama": "Budi", "alamat": "Jl. Test 1",
                                  "metode_pengiriman": "pickup", "nomor_hp": "628123456789",
                                  "payment_type": "full", "channel": "qris"})

    out = await checkout.finalize_order(WA)

    assert "gagal" in out.lower()
    assert "batas waktu" not in out.lower()
    assert cancelled, "pesanan yang tidak bisa dibayar harus ikut dibatalkan"
    assert await store.get_active_pending(WA) is None


async def test_backend_409_tells_the_customer_what_to_do(patch_externals):
    """Backend menolak pesanan kedua selama tagihan lama belum lunas. Pesan
    "coba ulangi sebentar lagi" mengirim pelanggan ke percobaan yang tidak akan
    pernah berhasil."""
    from app.conversation import checkout

    async def conflict(customer_id, items, metode_pengiriman, created_via="chatbot"):
        request = httpx.Request("POST", "http://backend/api/orders")
        response = httpx.Response(409, request=request,
                                 json={"detail": "Customer masih memiliki tagihan aktif."})
        raise httpx.HTTPStatusError("409", request=request, response=response)

    async def f_upsert(wa, nama, alamat, phone):
        return {"id": 1, "customer_id": 1}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "upsert_customer", f_upsert)
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "create_order", conflict)

    await store.set_cart(WA, [{"product_id": 5, "nama": "Brownies Coklat",
                               "harga": 50000.0, "qty": 1}])
    await store.set_customer(WA, {"nama": "Budi", "alamat": "Jl. Test 1",
                                  "metode_pengiriman": "pickup", "nomor_hp": "628123456789",
                                  "payment_type": "full", "channel": "qris"})

    out = await checkout.finalize_order(WA)

    assert "belum dibayar" in out.lower()
    assert "coba ulangi sebentar lagi" not in out.lower()


async def test_payment_instructions_can_be_sent_again(patch_externals):
    """Live: "kode qr nya kirim ulang dong" dijawab status pesanan tanpa tautan
    pembayaran, dan tidak ada tool lain yang bisa mengirimkannya."""
    from app.tools.payment_info import kirim_ulang_pembayaran

    await store.create_pending_order(
        wa_number=WA, order_ref="30001", payment_ref="MID", payment_type="dp",
        total_amount=100000, amount_due=50000, nomor_invoice="INV-TEST",
        pay_instruction="Scan QRIS: https://api.qr/mid-test",
        items_json="[]", customer_json="{}", delivery_method="pickup",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )
    set_turn_context(TurnContext(wa_number=WA))

    out = await kirim_ulang_pembayaran.ainvoke({})

    assert "https://api.qr/mid-test" in out
    assert "INV-TEST" in out
    assert "Rp50.000" in out


# ── Jumlah yang tidak pernah ditulis pelanggan ───────────────────────────────
async def test_quantity_the_customer_never_wrote_is_refused(patch_externals):
    """Live: "pesan bolu beberapa aja" -> add_to_cart(qty=2). Angka itu tidak
    pernah diketik siapa pun."""
    from app.tools.add_to_cart import add_to_cart

    set_turn_context(TurnContext(wa_number=WA, user_text="pesan bolu pandan beberapa aja"))
    out = await add_to_cart.ainvoke({"items": [{"product": "Bolu Pandan", "qty": 2}]})

    assert "jumlah" in out.lower()
    assert await store.get_cart(WA) == []

    set_turn_context(TurnContext(wa_number=WA, user_text="pesan bolu pandan 2 dong"))
    await add_to_cart.ainvoke({"items": [{"product": "Bolu Pandan", "qty": 2}]})
    assert (await store.get_cart(WA))[0]["qty"] == 2


# ── Dua pesan beruntun dari nomor yang sama ──────────────────────────────────
async def test_two_messages_at_once_do_not_double_the_cart(patch_externals):
    """Live: jempol kepencet dua kali -> keranjang jadi 4 dan pelanggan menerima
    dua ringkasan yang saling bertentangan (Rp380.000 dan Rp190.000)."""
    import asyncio

    from app.webhook import routes

    _mock_agent(patch_externals["monkeypatch"])

    order = []

    async def slow_handle(wa, text):
        from app.conversation.orchestrator import Reply
        order.append(("mulai", text))
        await asyncio.sleep(0.02)          # jendela balapan
        order.append(("selesai", text))
        return Reply(text="ok")

    patch_externals["monkeypatch"].setattr(routes, "handle_message", slow_handle)
    routes._locks.clear()

    await asyncio.gather(routes._process(WA, "pesan A"), routes._process(WA, "pesan B"))

    # Tanpa kunci, urutannya jadi mulai/mulai/selesai/selesai.
    assert [o[0] for o in order] == ["mulai", "selesai", "mulai", "selesai"], order


# ── Langkah deterministik tidak boleh jadi jalan buntu ───────────────────────
async def test_name_is_extracted_from_a_sentence(patch_externals):
    """Uji ulang di VM: "kan udah aku sebut di atas, Kevin" ditolak berulang kali
    dengan "Namanya sepertinya kurang tepat", dan percakapan mentok di situ."""
    from app.conversation.orchestrator import _extract_name

    assert _extract_name("kan udah aku sebut di atas, Kevin") == "Kevin"
    assert _extract_name("namaku Rina Kartika") == "Rina Kartika"
    assert _extract_name("Budi Santoso") == "Budi Santoso"
    assert _extract_name("aku cuma mau tanya tanya dulu sih kak sebenarnya") is None
    # Jawaban langkah berikutnya bukan nama: dulu tersimpan sebagai "dikirim".
    assert _extract_name("dikirim") is None
    assert _extract_name("qris") is None


async def test_question_at_confirmation_is_answered_then_reasked(patch_externals):
    """"berapa totalnya sekarang?" di langkah konfirmasi sempat dijawab daftar
    tombol yang sama berulang-ulang, bukan jawaban."""
    _mock_agent(patch_externals["monkeypatch"], "Totalnya Rp100.000.")
    await _cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])

    reply = await handle_message(WA, "berapa totalnya sekarang?")

    assert "Totalnya Rp100.000." in reply.text
    assert "sudah sesuai" in reply.text.lower()
    session = await store.get_or_create_session(WA)
    assert session.state == State.AWAITING_CART_CONFIRMATION
    assert (await store.get_cart(WA))[0]["qty"] == 2      # tetap tidak berubah


async def test_escalation_offer_still_stands_a_few_turns_later(patch_externals):
    """Live: "eh iya deh, sambungkan ke admin aja" dijawab dengan tawaran yang
    sama tanpa memanggil tool, sehingga "ya" berikutnya tidak punya apa pun untuk
    disetujui. Perbaikannya bukan menambah pencocokan kata kunci, melainkan
    membuat tawaran yang SUDAH diterbitkan model bertahan beberapa giliran."""
    from app.tools.escalate import escalate_to_admin

    _mock_agent(patch_externals["monkeypatch"], "Menu kami ada tiga kue.")
    set_turn_context(TurnContext(wa_number=WA, user_text="mau kue custom"))
    await store.log_message(
        WA, "out", await escalate_to_admin.ainvoke({"reason": "kue custom bertema frozen"}))

    await handle_message(WA, "menu dong")                 # menyela, bukan menolak
    assert await store.is_takeover_active(WA) is False

    reply = await handle_message(WA, "ya")                # baru setuju di sini
    assert "admin" in (reply.text or "").lower()
    assert await store.is_takeover_active(WA) is True
    assert any("628999000111" == wa for wa, _ in patch_externals["sent"])


async def test_unknown_product_answers_with_the_menu(patch_externals):
    """Rujukan yang tidak bisa disambungkan model ("yang coklat itu lho") adalah
    justru saat yang tepat menampilkan katalog, bukan menyuruh pelanggan
    "cek menu dulu"."""
    from app.tools.get_product_detail import get_product_detail

    set_turn_context(TurnContext(wa_number=WA, user_text="yang kemarin itu lho"))
    out = await get_product_detail.ainvoke({"product": "yang kemarin itu"})

    assert "Brownies Coklat" in out and "Bolu Pandan" in out
    assert "cek menu dulu" not in out.lower()


# ── FAQ: sumbernya /faq di backend, berkas lokal cuma cadangan ───────────────
async def test_faq_comes_from_backend_and_skips_inactive(patch_externals):
    """Admin mengedit jawaban bot lewat CRUD /faq di Admin Site, jadi tabel itu
    yang jadi sumber kebenaran. Item yang dinonaktifkan harus benar-benar hilang
    dari mulut bot, bukan sekadar tersembunyi di daftar."""
    import httpx as _httpx

    from app.rag import faq_source

    rows = [
        {"id": 2, "pertanyaan": "Berapa lama daya tahan kue?",
         "jawaban": "Tahan 3-4 hari di suhu ruang.", "is_active": True},
        {"id": 1, "pertanyaan": "Cara pesan?", "jawaban": "Chat WhatsApp.",
         "is_active": False},
        {"id": 3, "pertanyaan": "", "jawaban": "kosong", "is_active": True},
    ]

    def handler(request):
        assert request.url.path.endswith("/faq")
        return _httpx.Response(200, json=rows)

    real_client = _httpx.AsyncClient

    def factory(*a, **kw):
        kw.pop("timeout", None)
        return real_client(transport=_httpx.MockTransport(handler), **kw)

    patch_externals["monkeypatch"].setattr(faq_source.httpx, "AsyncClient", factory)

    docs = await faq_source.fetch_backend_faq()
    assert [d.source for d in docs] == ["backend-faq-2"]
    assert "daya tahan" in docs[0].text and "Tahan 3-4 hari" in docs[0].text


async def test_faq_falls_back_to_local_files_when_backend_has_none(patch_externals):
    """Instalasi baru yang FAQ backendnya masih kosong tidak boleh menjawab
    semua pertanyaan umum dengan "di luar cakupan"."""
    from app.rag import faq_source

    async def empty():
        return []

    patch_externals["monkeypatch"].setattr(faq_source, "fetch_backend_faq", empty)

    docs, asal = await faq_source.current_docs()
    assert asal == "berkas lokal"
    assert len(docs) >= 5 and all(d.source.endswith(".txt") for d in docs)


def test_faq_fingerprint_changes_when_an_answer_is_edited():
    """Refresh berkala membandingkan sidik jari ini; kalau tidak berubah saat
    teksnya berubah, editan admin tidak akan pernah ter-embed ulang."""
    from app.rag.faq_source import FaqDoc, fingerprint

    a = [FaqDoc("backend-faq-1", "Q: Buka jam berapa?\nA: 09.00-19.00")]
    b = [FaqDoc("backend-faq-1", "Q: Buka jam berapa?\nA: 10.00-20.00")]
    assert fingerprint(a) != fingerprint(b)
    assert fingerprint(a) == fingerprint(list(a))


async def test_faq_refresh_reingests_when_chroma_holds_another_source(patch_externals, tmp_path):
    """Container ingest dan service bisa melihat sumber FAQ yang berbeda — pernah
    terjadi: chatbot-ingest tidak punya BACKEND_BASE_URL lalu diam-diam
    meng-embed berkas .txt cadangan, sementara service membaca /faq backend
    dengan baik. Refresh harus mendamaikan itu, bukan menunggu FAQ berubah."""
    from app.conversation import background
    from app.rag import faq_source

    docs = [faq_source.FaqDoc("backend-faq-1", "Q: Buka jam berapa?\nA: 09.00-19.00")]

    async def from_backend():
        return docs, "backend"

    embedded = []

    patch_externals["monkeypatch"].setattr(faq_source, "current_docs", from_backend)
    patch_externals["monkeypatch"].setattr(
        faq_source, "ingest_documents", lambda d: embedded.append(d) or len(d))
    patch_externals["monkeypatch"].setattr(
        settings_module, "chroma_persist_dir", str(tmp_path), raising=False)

    background._last_faq_fingerprint = None
    faq_source.write_marker("sidik-jari-dari-berkas-lokal")

    assert await background._refresh_faq_if_changed() is True
    assert embedded == [docs]
    assert faq_source.read_marker() == faq_source.fingerprint(docs)

    # Sudah sinkron: pengecekan berikutnya tidak meng-embed apa pun lagi.
    embedded.clear()
    background._last_faq_fingerprint = None
    assert await background._refresh_faq_if_changed() is False
    assert embedded == []


async def test_terima_kasih_bukan_persetujuan_tawaran_admin(patch_externals):
    """Terukur hidup (suite W7): tawaran admin masih menggantung, pelanggan
    menulis "makasih ya kak", dan itu lolos sebagai konfirmasi karena "ya" ada di
    CONFIRM_WORDS — takeover menyala, lalu pesan berikutnya ("batal") dibungkam
    tanpa balasan apa pun. Ucapan terima kasih adalah penutup, bukan izin."""
    from app.tools.escalate import escalate_to_admin

    _mock_agent(patch_externals["monkeypatch"], "Sama-sama kak 😊")
    set_turn_context(TurnContext(wa_number=WA, user_text="mau kue custom"))
    await store.log_message(
        WA, "out", await escalate_to_admin.ainvoke({"reason": "kue custom"}))

    reply = await handle_message(WA, "makasih ya kak")

    assert await store.is_takeover_active(WA) is False
    assert patch_externals["sent"] == [], "tidak ada admin yang perlu diganggu"
    assert (reply.text or "").strip(), "pelanggan tetap harus dijawab"


async def test_tawaran_admin_kedaluwarsa_setelah_beberapa_giliran(patch_externals):
    """Tawaran yang menggantung lama tidak boleh diterima kata "ya" yang
    kebetulan lewat di kalimat lain."""
    from app.tools.escalate import escalate_to_admin

    _mock_agent(patch_externals["monkeypatch"], "Baik kak.")
    set_turn_context(TurnContext(wa_number=WA, user_text="mau kue custom"))
    await store.log_message(
        WA, "out", await escalate_to_admin.ainvoke({"reason": "kue custom"}))

    for pesan in ("menu dong", "harganya berapa", "ada rasa lain", "jam buka kapan"):
        await handle_message(WA, pesan)

    reply = await handle_message(WA, "ya udah kirim aja")

    assert await store.is_takeover_active(WA) is False
    assert (await store.get_or_create_session(WA)).pending_escalation is None
    assert (reply.text or "").strip()


async def test_metode_pengiriman_menerima_kalimat_sehari_hari(patch_externals):
    """Terukur hidup (suite W2): "dianter aja ke rumah" tidak cocok dengan kata
    "antar" — ejaan sehari-harinya "anter" — jadi pelanggan disuruh mengetik
    *delivery* padahal jawabannya sudah jelas."""
    from app.conversation.states import State

    for pesan, harapan in (
        ("dianter aja ke rumah", "delivery"),
        ("dikirim aja ya", "delivery"),
        ("pakai gosend", "delivery"),
        ("mau ambil sendiri di toko", "pickup"),
        ("aku mampir aja nanti", "pickup"),
    ):
        await store.set_customer(WA, {"nama": "Kevin", "alamat": "Jl. Mawar No. 3 Batam"})
        await store.set_state(WA, State.COLLECTING_IDENTITY)
        await handle_message(WA, pesan)
        cust = await store.get_customer(WA)
        assert cust.get("metode_pengiriman") == harapan, f"{pesan!r} -> {cust}"


async def test_jawaban_jumlah_bukan_izin_menyambungkan_ke_admin(patch_externals):
    """Terukur: tawaran admin menggantung, pelanggan menjawab jumlah kue dengan
    "dua ya", dan "ya" membuatnya dibaca sebagai persetujuan — bot bungkam."""
    from app.tools.escalate import escalate_to_admin

    _mock_agent(patch_externals["monkeypatch"], "Oke kak.")
    set_turn_context(TurnContext(wa_number=WA, user_text="mau kue custom"))
    await store.log_message(
        WA, "out", await escalate_to_admin.ainvoke({"reason": "kue custom"}))

    await handle_message(WA, "dua ya")

    assert await store.is_takeover_active(WA) is False
    assert patch_externals["sent"] == []


async def test_keluhan_dijawab_permintaan_maaf_yang_tetap(patch_externals):
    """Terukur: "kuenya kemarin basi, aku kecewa banget" dijawab "Wah, makasih
    banyak kak! Senang banget kalau suka 😊". Nada keluhan terlalu mahal untuk
    diserahkan ke karangan model 1,7 B, jadi kalimatnya tetap — yang tetap jadi
    keputusan model hanyalah kapan tool ini dipakai."""
    from app.tools.keluhan import sampaikan_maaf

    set_turn_context(TurnContext(wa_number=WA, user_text="kuenya basi"))
    out = await sampaikan_maaf.ainvoke({"keluhan": "kue diterima sudah basi"})

    assert out.startswith("Mohon maaf sekali atas ketidaknyamanan yang dialami.")
    assert "bahan perbaikan" in out
    # Pelanggan yang sedang kecewa tidak dimintai cerita ulang.
    assert "nomor pesanan" not in out.lower() and "fotonya" not in out.lower()
    assert await store.is_takeover_active(WA) is False, "keluhan tidak membungkam bot"


async def test_tool_keluhan_terdaftar_untuk_model(patch_externals):
    """Tool yang tidak terdaftar tidak akan pernah dipanggil model."""
    from app.tools.registry import TOOLS_BY_NAME

    assert "sampaikan_maaf" in TOOLS_BY_NAME


async def test_balasan_keluhan_menyebut_email_kalau_disetel(patch_externals):
    """Alamat tindak lanjut diambil dari setelan, dan tidak pernah dikarang."""
    from app.core.config import settings
    from app.tools import keluhan

    set_turn_context(TurnContext(wa_number=WA, user_text="kuenya basi"))
    patch_externals["monkeypatch"].setattr(
        settings_module, "store_support_email", "halo@toticakery.id", raising=False)
    out = await keluhan.sampaikan_maaf.ainvoke({"keluhan": "kue basi"})
    assert "halo@toticakery.id" in out

    patch_externals["monkeypatch"].setattr(
        settings, "store_support_email", "", raising=False)
    out = await keluhan.sampaikan_maaf.ainvoke({"keluhan": "kue basi"})
    assert "@" not in out, "tanpa setelan, jangan menyebut alamat apa pun"


async def test_add_to_cart_menerima_argumen_tanpa_bungkus_items(patch_externals):
    """Terukur pada toti-qwen-1.7b-v6b: giliran "satu aja" memanggil add_to_cart
    dengan {"product": "Brownies Coklat", "qty": 1} — produk dan jumlahnya sudah
    benar, cuma tanpa bungkus `items`. Ditolak mentah, pelanggan menerima "Maaf,
    ada kendala saat memproses permintaanmu" untuk maksud yang sudah terbaca."""
    from app.tools.add_to_cart import add_to_cart

    set_turn_context(TurnContext(wa_number=WA, user_text="satu aja"))
    out = await add_to_cart.ainvoke({"product": "Brownies Coklat", "qty": 1})

    keranjang = await store.get_cart(WA)
    assert len(keranjang) == 1 and keranjang[0]["qty"] == 1
    assert "Brownies Coklat" in out


async def test_add_to_cart_tanpa_produk_menampilkan_menu(patch_externals):
    """Argumen kosong tidak boleh jadi galat teknis di depan pelanggan."""
    from app.tools.add_to_cart import add_to_cart

    set_turn_context(TurnContext(wa_number=WA, user_text="mau pesan"))
    out = await add_to_cart.ainvoke({"items": []})

    assert await store.get_cart(WA) == []
    assert "Brownies Coklat" in out, "jatuhnya ke daftar menu, bukan pesan galat"


async def test_refund_admin_mengosongkan_pesanan_lokal_dan_mengabari_pelanggan(patch_externals):
    """Backend punya POST /orders/{id}/refund (khusus Admin/Owner): invoice jadi
    Refunded dan pesanan induk Cancelled. Tanpa pemeriksaan ini chatbot tidak
    pernah tahu — pelanggan tidak dikabari dananya kembali, dan baris pesanan
    lokalnya tetap 'aktif' sehingga dia diblokir memesan lagi."""
    from app.conversation import background

    pesanan = await store.create_pending_order(
        wa_number=WA, order_ref="77", payment_ref="MID", payment_type="full",
        total_amount=150000, amount_due=150000, items_json="[]", customer_json="{}",
        delivery_method="pickup", status="paid", nomor_invoice="INV-20260912-77",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )

    async def f_status(order_ref):
        return {"invoice_status": "refunded", "amount_paid": 0, "amount_due": 0}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    await background._check_refunded()

    assert await store.get_active_pending(WA) is None, "pelanggan harus bisa memesan lagi"
    assert (await store.get_or_create_session(WA)).state == State.IDLE
    kabar = [t for _wa, t in patch_externals["sent"]]
    assert kabar and "dikembalikan" in kabar[-1]
    assert "INV-20260912-77" in kabar[-1]
    assert pesanan.id


async def test_tool_status_pembayaran_mengenali_refund(patch_externals):
    """"Sudah saya bayar?" untuk pesanan yang sudah di-refund tidak boleh dijawab
    "pembayaranmu belum terdeteksi" — benar secara harfiah, menyesatkan nyatanya."""
    from app.tools.payment_status import check_payment_status

    await store.create_pending_order(
        wa_number=WA, order_ref="78", payment_ref="MID", payment_type="full",
        total_amount=90000, amount_due=90000, items_json="[]", customer_json="{}",
        delivery_method="pickup", status="paid", nomor_invoice="INV-20260912-78",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )

    async def f_status(order_ref):
        return {"invoice_status": "refunded", "amount_paid": 0, "amount_due": 0}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)
    set_turn_context(TurnContext(wa_number=WA, user_text="udah aku bayar kok"))

    out = await check_payment_status.ainvoke({})

    assert "dikembalikan" in out and "belum terdeteksi" not in out


async def test_batal_setelah_bayar_minta_konfirmasi_dulu(patch_externals):
    """Uang pelanggan tidak berpindah karena satu kalimat: pesanan yang sudah
    dibayar dijawab pertanyaan tertutup dulu, bukan langsung dibatalkan."""
    from app.tools.cancel_order import cancel_order

    await _seed_awaiting_payment(order_ref="9101")
    pesanan = await store.get_active_pending(WA)
    await store.update_pending_order(pesanan.id, status="paid")

    async def f_latest(wa):
        return {"id": 9101, "status": "pending"}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_latest_order", f_latest)
    set_turn_context(TurnContext(wa_number=WA, user_text="batalin pesananku"))

    out = await cancel_order.ainvoke({})

    assert "*ya*" in out and "dana" in out.lower()
    assert await store.get_active_pending(WA) is not None, "belum boleh dibatalkan"


async def test_konfirmasi_batal_tanpa_dukungan_backend_tidak_membuntukan(patch_externals):
    """Selama backend belum mengizinkan chatbot me-refund, jawaban "ya" tidak
    boleh berakhir buntu — pelanggan diberi alamat yang benar-benar ditangani."""
    from app.tools.cancel_order import proses_pembatalan

    await _seed_awaiting_payment(order_ref="9102")
    pesanan = await store.get_active_pending(WA)
    await store.update_pending_order(pesanan.id, status="paid")

    async def f_cancel(order_ref):
        raise httpx.HTTPStatusError("409", request=None, response=None)

    async def f_refund(order_ref, reason, wa_number=""):
        return None

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "cancel_order", f_cancel)
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "refund_order", f_refund)
    patch_externals["monkeypatch"].setattr(
        settings_module, "store_support_email", "halo@toticakery.id", raising=False)

    out = await proses_pembatalan(WA)

    assert "halo@toticakery.id" in out
    assert "hubungi admin" not in out.lower()
    assert await store.get_active_pending(WA) is not None, "tidak ada yang dibatalkan"


async def test_konfirmasi_batal_berhasil_saat_backend_mengizinkan(patch_externals):
    """Begitu backend mengizinkan (cancel berbayar ATAU refund lewat service key),
    jalurnya langsung hidup tanpa perubahan kode lagi."""
    from app.tools.cancel_order import proses_pembatalan

    await _seed_awaiting_payment(order_ref="9103")
    pesanan = await store.get_active_pending(WA)
    await store.update_pending_order(pesanan.id, status="paid")

    dipanggil = []

    async def f_cancel(order_ref):
        raise httpx.HTTPStatusError("409", request=None, response=None)

    async def f_refund(order_ref, reason, wa_number=""):
        dipanggil.append((order_ref, wa_number))
        return {"id": int(order_ref), "status": "cancelled"}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "cancel_order", f_cancel)
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "refund_order", f_refund)

    out = await proses_pembatalan(WA)

    assert dipanggil and dipanggil[0][0] == "9103"
    assert "dibatalkan" in out and "pengembalian dananya" in out
    assert await store.get_active_pending(WA) is None
    assert (await store.get_or_create_session(WA)).state == State.IDLE


async def test_pesanan_yang_sudah_diproses_tidak_ditawari_pembatalan(patch_externals):
    """Kebijakan toko: refund mandiri hanya selama pesanan masih `pending`.
    Perpindahan ke `in_process` dilakukan admin manual, dan itu penanda kuenya
    mulai dikerjakan — jangan menawarkan pembatalan yang pasti ditolak backend."""
    from app.tools.cancel_order import cancel_order

    await _seed_awaiting_payment(order_ref="9104")
    pesanan = await store.get_active_pending(WA)
    await store.update_pending_order(pesanan.id, status="paid")

    async def f_latest(wa):
        return {"id": 9104, "status": "in_process"}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_latest_order", f_latest)
    patch_externals["monkeypatch"].setattr(
        settings_module, "store_support_email", "halo@toticakery.id", raising=False)
    set_turn_context(TurnContext(wa_number=WA, user_text="batalin pesananku"))

    out = await cancel_order.ainvoke({})

    assert "tahap pengerjaan sehingga tidak bisa dibatalkan" in out
    assert "halo@toticakery.id" in out
    assert "*ya*" not in out, "jangan menawarkan konfirmasi yang pasti gagal"
    assert (await store.get_or_create_session(WA)).state != State.AWAITING_CANCEL_CONFIRMATION


async def test_pesanan_belum_dibayar_juga_dikonfirmasi_dulu(patch_externals):
    """Kebijakan 12 Sep: SETIAP pembatalan pesanan ditanyakan dulu, termasuk yang
    belum dibayar — pesanannya sudah tercatat di backend dan Admin Site."""
    from app.tools.cancel_order import cancel_order, proses_pembatalan

    await _seed_awaiting_payment(order_ref="9105")
    dibatalkan = []

    async def f_cancel(order_ref):
        dibatalkan.append(order_ref)
        return {"status": "success"}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "cancel_order", f_cancel)
    set_turn_context(TurnContext(wa_number=WA, user_text="batalin pesananku"))

    tanya = await cancel_order.ainvoke({})
    assert "*ya*" in tanya
    assert "pengembalian dana" not in tanya, "belum dibayar: jangan menjanjikan refund"
    assert not dibatalkan, "belum boleh dibatalkan sebelum dijawab"

    hasil = await proses_pembatalan(WA)
    assert dibatalkan == ["9105"]
    assert "dibatalkan" in hasil.lower()
    assert "dana" not in hasil.lower(), "belum dibayar: jangan menjanjikan pengembalian"
    assert await store.get_active_pending(WA) is None


async def test_webhook_refund_mengabari_pelanggan_tanpa_menunggu_polling(patch_externals):
    """Polling 30 detik itu jaring pengaman, bukan satu-satunya jalan: backend
    bisa menembak webhook internal begitu refund selesai, persis pola /ready."""
    from app.conversation import background

    await store.create_pending_order(
        wa_number=WA, order_ref="90", payment_ref="MID", payment_type="dp",
        total_amount=120000, amount_due=60000, items_json="[]", customer_json="{}",
        delivery_method="pickup", status="paid", nomor_invoice="INV-20260912-90",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
    )

    async def f_status(order_ref):
        return {"invoice_status": "refunded", "amount_paid": 0, "amount_due": 0}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    ok = await background.notify_refunded(90)

    assert ok is True
    assert await store.get_active_pending(WA) is None
    kabar = [t for _wa, t in patch_externals["sent"]]
    assert kabar and "dikembalikan" in kabar[-1]

    # Pesanan yang tidak dikenal tidak bikin galat, cuma dilaporkan not_found.
    assert await background.notify_refunded(999999) is False


async def test_kabar_refund_gagal_kirim_dicoba_lagi(patch_externals):
    """Kalau pesannya gagal terkirim, pesanan JANGAN ditandai selesai — kalau
    ditandai, pelanggan yang uangnya dikembalikan tidak pernah diberi tahu dan
    tidak ada yang mengulang."""
    from app.conversation import background

    await store.create_pending_order(
        wa_number=WA, order_ref="91", payment_ref="MID", payment_type="dp",
        total_amount=80000, amount_due=40000, items_json="[]", customer_json="{}",
        delivery_method="pickup", status="paid", nomor_invoice="INV-20260912-91",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
    )

    async def f_status(order_ref):
        return {"invoice_status": "refunded", "amount_paid": 0, "amount_due": 0}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    from app.whatsapp_client.client import whatsapp_client

    async def kirim_gagal(wa, text):
        raise httpx.HTTPStatusError("404 session", request=None, response=None)

    patch_externals["monkeypatch"].setattr(whatsapp_client, "send_text", kirim_gagal)

    assert await background.notify_refunded(91) is False
    assert await store.get_active_pending(WA) is not None, "belum dikabari, jangan ditutup"

    # Gateway pulih -> percobaan berikutnya berhasil dan barulah ditutup.
    async def kirim_ok(wa, text):
        patch_externals["sent"].append((wa, text))
        return {"ok": True}

    patch_externals["monkeypatch"].setattr(whatsapp_client, "send_text", kirim_ok)
    assert await background.notify_refunded(91) is True
    assert await store.get_active_pending(WA) is None


async def test_webhook_pembayaran_mengabari_tanpa_menunggu_polling(patch_externals):
    """Pembayaran adalah kabar yang paling ditunggu pelanggan — begitu QRIS-nya
    discan, dia menatap layar. Webhook dari backend memangkas jeda 30 detik itu,
    tapi statusnya tetap dipastikan ke backend dulu."""
    from app.conversation import background

    await _seed_awaiting_payment(order_ref="9201")

    async def f_status(order_ref):
        return {"invoice_status": "paid", "amount_paid": 100000, "amount_due": 0}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    assert await background.notify_paid(9201) is True
    kabar = [t for _wa, t in patch_externals["sent"]]
    assert kabar and "Pembayaran sudah kami terima" in kabar[-1]
    assert (await store.get_or_create_session(WA)).state == State.ORDER_ACTIVE
    # Panggilan kedua tidak mengirim kabar dua kali.
    assert await background.notify_paid(9201) is False


async def test_webhook_pembayaran_menolak_kalau_backend_bilang_belum_lunas(patch_externals):
    """Webhook cuma pemicu, bukan sumber kebenaran soal uang."""
    from app.conversation import background

    await _seed_awaiting_payment(order_ref="9202")

    async def f_status(order_ref):
        return {"invoice_status": "unpaid", "amount_paid": 0, "amount_due": 100000}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    assert await background.notify_paid(9202) is False
    assert patch_externals["sent"] == []


async def test_webhook_refund_ditolak_kalau_backend_bilang_belum_refund(patch_externals):
    """Webhook refund cuma pemicu. Tanpa memastikan ke backend, satu panggilan
    keliru cukup untuk memberi tahu pelanggan "dananya dikembalikan" padahal
    tidak ada uang yang kembali, dan pesanannya telanjur ditutup."""
    from app.conversation import background

    await store.create_pending_order(
        wa_number=WA, order_ref="92", payment_ref="MID", payment_type="dp",
        total_amount=80000, amount_due=40000, items_json="[]", customer_json="{}",
        delivery_method="pickup", status="paid", nomor_invoice="INV-20260912-92",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2),
    )

    async def f_status(order_ref):
        return {"invoice_status": "paid", "amount_paid": 40000, "amount_due": 40000}

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_payment_status", f_status)

    assert await background.notify_refunded(92) is False
    assert patch_externals["sent"] == []
    assert await store.get_active_pending(WA) is not None


async def test_refund_va_dikatakan_manual_qris_otomatis(patch_externals):
    """VA tidak bisa di-refund otomatis oleh Midtrans (terukur: status 418),
    sedangkan QRIS bisa. Pelanggan harus diberi tahu bedanya — yang manual
    butuh beberapa hari kerja karena ada orang yang harus transfer."""
    from app.tools.cancel_order import cancel_order, proses_pembatalan

    async def f_cancel(order_ref):
        raise httpx.HTTPStatusError("409", request=None, response=None)

    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "cancel_order", f_cancel)
    patch_externals["monkeypatch"].setattr(
        settings_module, "store_support_email", "halo@toticakery.id", raising=False)

    for channel, mode, harus_ada, tidak_boleh in (
        ("bank_transfer", "manual", "transfer", "otomatis"),
        ("qris", "auto", "otomatis", "beberapa hari kerja"),
    ):
        await store.set_customer(WA, {"nama": "Kevin", "alamat": "Jl. Mawar",
                                      "metode_pengiriman": "pickup", "channel": channel})
        await store.create_pending_order(
            wa_number=WA, order_ref=f"93{channel[:2]}", payment_ref="MID",
            payment_type="full", total_amount=100000, amount_due=100000,
            items_json="[]", customer_json=json.dumps({"channel": channel}),
            delivery_method="pickup", status="paid", nomor_invoice=f"INV-{channel}",
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
        )

        async def f_refund(order_ref, reason, wa_number="", _m=mode):
            return {"order_id": order_ref, "status": "cancelled", "refund_mode": _m}

        patch_externals["monkeypatch"].setattr(
            patch_externals["backend"], "refund_order", f_refund)
        set_turn_context(TurnContext(wa_number=WA, user_text="batalin pesananku"))

        tanya = await cancel_order.ainvoke({})
        hasil = await proses_pembatalan(WA)

        assert harus_ada in hasil.lower(), (channel, hasil)
        assert tidak_boleh not in hasil.lower(), (channel, hasil)
        if channel == "bank_transfer":
            assert "transfer" in tanya.lower(), "konfirmasinya pun harus jujur di awal"
            assert "halo@toticakery.id" in hasil
