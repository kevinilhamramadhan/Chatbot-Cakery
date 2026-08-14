"""End-to-end behaviour tests for the conversation flow (no Ollama, no backend).

The LLM and backend HTTP (app.backend_client.api) are mocked; everything else is
the real code path.
"""

import datetime as dt
import json

import pytest

from app.conversation import background, store
from app.conversation.context import TurnContext, set_turn_context
from app.conversation.orchestrator import handle_message
from app.conversation.states import State

WA = "628123456789@c.us"

FAKE_PRODUCTS = [
    {"id": 5, "nama_produk": "Brownies Coklat", "deskripsi": "Brownies fudgy",
     "kategori": "cake", "harga_jual": 50000, "image_url": "http://img/5.jpg", "is_active": True},
    {"id": 8, "nama_produk": "Bolu Pandan", "deskripsi": "Bolu lembut",
     "kategori": "cake", "harga_jual": 75000, "image_url": None, "is_active": True},
]


@pytest.fixture(autouse=True)
def patch_externals(monkeypatch):
    """Mock product reads, the backend API client, and WhatsApp sends."""
    from app.backend_client import api as backend
    from app.backend_client import products as products_api
    from app.whatsapp_client.client import whatsapp_client

    async def fake_list(only_active=True, kategori=None):
        return [p for p in FAKE_PRODUCTS if (not kategori or p["kategori"] == kategori)]

    async def fake_get(pid):
        return next((p for p in FAKE_PRODUCTS if p["id"] == pid), None)

    monkeypatch.setattr(products_api, "list_products", fake_list)
    monkeypatch.setattr(products_api, "get_product", fake_get)

    sent = []

    async def fake_send_text(wa, text):
        sent.append((wa, text))
        return {"ok": True}

    monkeypatch.setattr(whatsapp_client, "send_text", fake_send_text)

    # Backend API stubs (sane defaults; individual tests override).
    async def f_upsert(wa, nama, alamat, phone):
        return {"id": 1, "customer_id": 1, "nomor_wa": wa, "nama": nama, "alamat": alamat}

    async def f_create_order(customer_id, items, metode_pengiriman, created_via="chatbot"):
        return {"order_id": 30001, "nomor_invoice": "INV-TEST",
                "total_harga_pesanan": 100000, "status": "pending"}

    charges = []

    async def f_create_payment(order_id, amount, channel="bank_transfer", payment_type="full"):
        charges.append({"order_id": order_id, "amount": amount,
                        "channel": channel, "payment_type": payment_type})
        if channel == "qris":
            return {"payment_id": 1, "pg_transaction_id": "MID",
                    "va_number": None, "qris_url": "https://api.qr/mid-test", "status": "Pending"}
        return {"payment_id": 1, "pg_transaction_id": "MID",
                "va_number": "8808123456789012", "qris_url": None, "status": "Pending"}

    async def f_payment_status(order_id):
        return {"invoice_status": "unpaid", "amount_paid": 0, "amount_due": 0, "payments": []}

    async def f_latest(wa):
        return None

    async def f_cancel(order_id):
        return {"status": "success"}

    async def f_set_takeover(wa, active, expires_at):
        return {"nomor_wa": wa, "human_takeover_active": active}

    async def f_admin():
        return []

    async def f_takeover_status(wa):
        # Mirror the local flag so suppress tests behave like a synced backend.
        return {"nomor_wa": wa, "human_takeover_active": await store.is_takeover_active(wa),
                "is_expired": False}

    for name, fn in {"upsert_customer": f_upsert, "create_order": f_create_order,
                     "create_payment": f_create_payment, "get_payment_status": f_payment_status,
                     "get_latest_order": f_latest, "cancel_order": f_cancel,
                     "set_takeover": f_set_takeover, "get_takeover_admin_numbers": f_admin,
                     "get_takeover_status": f_takeover_status}.items():
        monkeypatch.setattr(backend, name, fn)
    return {"sent": sent, "backend": backend, "monkeypatch": monkeypatch,
            "charges": charges}


async def _seed_cart_awaiting_confirmation(items):
    from app.tools.add_to_cart import add_to_cart
    set_turn_context(TurnContext(wa_number=WA))
    out = await add_to_cart.ainvoke({"items": items})
    await store.set_state(WA, State.AWAITING_CART_CONFIRMATION)
    return out


# ── add_to_cart ───────────────────────────────────────────────────────────────
async def test_add_to_cart_resolves_price_and_merges():
    out = await _seed_cart_awaiting_confirmation(
        [{"product": "Brownies Coklat", "qty": 2}, {"product": "brownies", "qty": 1}]
    )
    cart = await store.get_cart(WA)
    assert len(cart) == 1 and cart[0]["qty"] == 3 and cart[0]["harga"] == 50000
    assert "Rp150.000" in out


async def test_add_to_cart_unknown_product():
    set_turn_context(TurnContext(wa_number=WA))
    from app.tools.add_to_cart import add_to_cart
    out = await add_to_cart.ainvoke({"items": [{"product": "Pizza", "qty": 1}]})
    assert "tidak menemukan" in out.lower()
    assert await store.get_cart(WA) == []


async def test_add_to_cart_enforces_minimum_order(monkeypatch):
    """products.minimum_order exists in the catalogue but POST /orders does NOT
    enforce it — without this check the customer gets an invoice for a quantity
    the store won't bake. Never silently bump the qty: ask instead."""
    from app.backend_client import products as products_api
    from app.tools.add_to_cart import add_to_cart
    bulk = {"id": 77, "nama_produk": "Mini Cookies 7cm", "harga_jual": 25000,
            "is_active": True, "minimum_order": 5}
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async([bulk]))
    monkeypatch.setattr(products_api, "get_product",
                        lambda pid: _async(bulk if pid == 77 else None))
    set_turn_context(TurnContext(wa_number=WA))

    out = await add_to_cart.ainvoke({"items": [{"product": "Mini Cookies 7cm", "qty": 2}]})
    assert "minimal 5" in out
    assert await store.get_cart(WA) == []          # nothing added, nothing bumped

    out = await add_to_cart.ainvoke({"items": [{"product": "Mini Cookies 7cm", "qty": 5}]})
    cart = await store.get_cart(WA)
    assert len(cart) == 1 and cart[0]["qty"] == 5


async def test_add_to_cart_minimum_counts_existing_lines(monkeypatch):
    """3 + 2 of the same product clears a minimum of 5 — the check is on the
    resulting line total, not on each message in isolation."""
    from app.backend_client import products as products_api
    from app.tools.add_to_cart import add_to_cart
    bulk = {"id": 77, "nama_produk": "Mini Cookies 7cm", "harga_jual": 25000,
            "is_active": True, "minimum_order": 5}
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async([bulk]))
    set_turn_context(TurnContext(wa_number=WA))
    await store.set_cart(WA, [{"product_id": 77, "nama": "Mini Cookies 7cm",
                               "harga": 25000.0, "qty": 3}])
    await add_to_cart.ainvoke({"items": [{"product": "Mini Cookies 7cm", "qty": 2}]})
    cart = await store.get_cart(WA)
    assert len(cart) == 1 and cart[0]["qty"] == 5


async def test_add_to_cart_rejects_unavailable(monkeypatch):
    from app.backend_client import products as products_api
    from app.tools.add_to_cart import add_to_cart
    habis = {"id": 99, "nama_produk": "Kue Habis", "harga_jual": 40000,
             "is_available": False, "is_active": True}
    monkeypatch.setattr(products_api, "list_products", lambda only_active=True, kategori=None: _async([habis]))
    monkeypatch.setattr(products_api, "get_product", lambda pid: _async(habis if pid == 99 else None))
    set_turn_context(TurnContext(wa_number=WA))
    out = await add_to_cart.ainvoke({"items": [{"product": "Kue Habis", "qty": 1}]})
    assert "tidak tersedia" in out.lower()
    assert await store.get_cart(WA) == []


async def test_add_to_cart_asks_when_product_ambiguous(monkeypatch):
    """Regression (live WA test): 'beli 4 cupcake' silently became 'Cupcakes
    isi 6 x4'. Ambiguous names must trigger a question, never a guess."""
    from app.backend_client import products as products_api
    from app.tools.add_to_cart import add_to_cart
    cups = [
        {"id": 16, "nama_produk": "Cupcakes isi 4", "harga_jual": 40000, "is_active": True},
        {"id": 17, "nama_produk": "Cupcakes isi 6", "harga_jual": 55000, "is_active": True},
        {"id": 18, "nama_produk": "Cupcakes isi 9", "harga_jual": 80000, "is_active": True},
    ]
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async(cups))
    set_turn_context(TurnContext(wa_number=WA))
    out = await add_to_cart.ainvoke({"items": [{"product": "cupcake", "qty": 4}]})
    assert await store.get_cart(WA) == []                    # nothing guessed in
    assert "isi 4" in out and "isi 6" in out and "isi 9" in out
    assert "mana" in out.lower()                             # asks the customer

    # A specific answer resolves normally.
    out = await add_to_cart.ainvoke({"items": [{"product": "cupcakes isi 4", "qty": 4}]})
    cart = await store.get_cart(WA)
    assert len(cart) == 1 and cart[0]["product_id"] == 16 and cart[0]["qty"] == 4


async def test_product_detail_prefixes_relative_image_path(monkeypatch):
    """Backend stores /static/... paths; the chatbot must prefix its own
    BACKEND_BASE_URL so the wwebjs container can actually fetch the image."""
    from app.backend_client import products as products_api
    from app.core.config import settings
    from app.tools.get_product_detail import get_product_detail
    p12 = {"id": 12, "nama_produk": "Cake 10cm", "harga_jual": 90000,
           "is_active": True, "image_url": "/static/products/12.jpg"}
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async([p12]))
    # Reachability is a separate concern (covered below); assume the URL serves.
    monkeypatch.setattr("app.tools.get_product_detail._image_exists",
                        lambda url: _async(True))
    ctx = TurnContext(wa_number=WA)
    set_turn_context(ctx)
    await get_product_detail.ainvoke({"product": "cake 10cm"})
    assert len(ctx.media) == 1
    expected = settings.backend_base_url.rstrip("/") + "/static/products/12.jpg"
    assert ctx.media[0].image_url == expected
    # Absolute URLs (e.g. an old Cloudinary entry) pass through untouched.
    p12["image_url"] = "https://cdn.example.com/12.jpg"
    ctx2 = TurnContext(wa_number=WA)
    set_turn_context(ctx2)
    await get_product_detail.ainvoke({"product": "cake 10cm"})
    assert ctx2.media[0].image_url == "https://cdn.example.com/12.jpg"


async def test_product_detail_skips_unreachable_image(monkeypatch):
    """The backend on Vercel loses /static on redeploy (verified 404), and a dead
    URL makes wwebjs-api throw after an otherwise fine reply. Text still goes."""
    from app.backend_client import products as products_api
    from app.tools.get_product_detail import get_product_detail
    p12 = {"id": 12, "nama_produk": "Cake 10cm", "harga_jual": 90000,
           "is_active": True, "image_url": "/static/products/12.jpg"}
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async([p12]))
    monkeypatch.setattr("app.tools.get_product_detail._image_exists",
                        lambda url: _async(False))
    ctx = TurnContext(wa_number=WA)
    set_turn_context(ctx)
    out = await get_product_detail.ainvoke({"product": "cake 10cm"})
    assert ctx.media == []                 # no broken media queued
    assert "Rp90.000" in out               # the text answer is unaffected


async def test_product_detail_asks_when_ambiguous(monkeypatch):
    from app.backend_client import products as products_api
    from app.tools.get_product_detail import get_product_detail
    cups = [
        {"id": 16, "nama_produk": "Cupcakes isi 4", "harga_jual": 40000, "is_active": True},
        {"id": 17, "nama_produk": "Cupcakes isi 6", "harga_jual": 55000, "is_active": True},
    ]
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async(cups))
    set_turn_context(TurnContext(wa_number=WA))
    out = await get_product_detail.ainvoke({"product": "cupcake"})
    assert "isi 4" in out and "isi 6" in out and "mana" in out.lower()


async def _async(v):
    return v


# ── Full happy path: confirm -> identity -> DP -> payment ─────────────────────
async def test_full_order_flow_with_dp():
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])
    r = await handle_message(WA, "sudah sesuai")
    assert (await store.get_or_create_session(WA)).state == State.COLLECTING_IDENTITY
    await handle_message(WA, "Budi Santoso")
    await handle_message(WA, "Jl. Mawar No. 10, Batam")
    await handle_message(WA, "delivery")
    await handle_message(WA, "ya")
    r = await handle_message(WA, "dp")
    assert "qris" in r.text.lower()              # channel prompt (VA vs QRIS)
    r = await handle_message(WA, "va")
    assert "8808123456789012" in r.text          # VA from backend charge
    assert "Rp50.000" in r.text                  # DP 50% of 100000

    session = await store.get_or_create_session(WA)
    assert session.state == State.AWAITING_PAYMENT
    order = await store.get_active_pending(WA)
    assert order.payment_type == "dp" and order.total_amount == 100000 and order.amount_due == 50000
    assert order.order_ref == "30001"            # backend order id tracked locally
    assert json.loads(order.customer_json)["nomor_hp"] == "628123456789"


async def test_charge_sends_payment_method_and_type(patch_externals):
    """Backend commit 3409fc7 split the body: `payment_method` (bank_transfer|
    qris) vs `payment_type` (full|dp), and now recomputes the expected amount
    from the order — sending the old shape gets a 422, a wrong amount a 400."""
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "dp", "qris"):
        await handle_message(WA, msg)

    charge = patch_externals["charges"][-1]
    assert charge["channel"] == "qris"          # -> payment_method
    assert charge["payment_type"] == "dp"       # -> payment_type
    assert charge["amount"] == 50000.0          # exactly total * 0.5 (100000)


async def test_dp_amount_keeps_cents_on_odd_total(patch_externals, monkeypatch):
    """The backend compares against (total * 0.5).quantize(0.01); rounding to
    whole rupiah here would 400 on any odd total."""
    from app.backend_client import products as products_api
    odd = {"id": 5, "nama_produk": "Brownies Coklat", "harga_jual": 50001,
           "is_active": True}
    monkeypatch.setattr(products_api, "list_products",
                        lambda only_active=True, kategori=None: _async([odd]))
    monkeypatch.setattr(products_api, "get_product",
                        lambda pid: _async(odd if pid == 5 else None))

    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "dp", "va"):
        await handle_message(WA, msg)
    assert patch_externals["charges"][-1]["amount"] == 25000.5


async def test_checkout_reprices_stale_cart_and_reconfirms(patch_externals):
    """Security fix: the charged amount used to come from the price snapshotted
    at add_to_cart time, so a cart parked in the session could be checked out at
    an old price. Checkout must re-read the live price and re-confirm."""
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 2}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "full"):
        await handle_message(WA, msg)

    FAKE_PRODUCTS[0]["harga_jual"] = 60000          # price went up mid-session
    try:
        r = await handle_message(WA, "va")
        assert await store.get_active_pending(WA) is None   # nothing charged yet
        assert "Rp120.000" in r.text and "Rp50.000" in r.text  # new total + old price
        assert (await store.get_or_create_session(WA)).state == State.AWAITING_CART_CONFIRMATION

        # Re-confirming charges the NEW price without re-asking for identity.
        r = await handle_message(WA, "sudah sesuai")
        assert "8808123456789012" in r.text
        order = await store.get_active_pending(WA)
        assert order.total_amount == 120000 and order.amount_due == 120000
    finally:
        FAKE_PRODUCTS[0]["harga_jual"] = 50000


async def test_checkout_drops_item_that_went_unavailable(patch_externals):
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "full"):
        await handle_message(WA, msg)

    FAKE_PRODUCTS[0]["is_available"] = False
    try:
        r = await handle_message(WA, "va")
        assert "tidak tersedia" in r.text.lower()
        assert await store.get_active_pending(WA) is None
        assert await store.get_cart(WA) == []
    finally:
        FAKE_PRODUCTS[0].pop("is_available")


async def test_retention_purge_clears_old_transcripts(patch_externals):
    from sqlalchemy import text as sql_text

    from app.core.database import async_session_factory

    await store.log_message(WA, "in", "alamat rumahku Jl. Rahasia 1")
    async with async_session_factory() as db:      # backdate past the retention window
        await db.execute(sql_text(
            "UPDATE chatbot_conversations SET created_at = '2020-01-01 00:00:00'"))
        await db.commit()

    logs, _ = await store.purge_old_data()
    assert logs == 1
    assert await store.recent_history(WA) == []


async def test_payment_failure_cancels_backend_order(patch_externals):
    """If the Midtrans charge fails after the order was created, the backend
    order must be cancelled (no orphaned pending orders piling up on retry)."""
    cancelled = []

    async def boom(order_id, amount, channel="bank_transfer"):
        raise RuntimeError("midtrans down")

    async def f_cancel(order_id):
        cancelled.append(order_id)
        return {"status": "success"}

    mp, backend = patch_externals["monkeypatch"], patch_externals["backend"]
    mp.setattr(backend, "create_payment", boom)
    mp.setattr(backend, "cancel_order", f_cancel)

    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "full"):
        await handle_message(WA, msg)
    r = await handle_message(WA, "va")
    assert "gagal" in r.text.lower()
    assert cancelled == [30001]                      # backend order cleaned up
    assert await store.get_active_pending(WA) is None


async def test_full_payment_charges_full_amount():
    await _seed_cart_awaiting_confirmation([{"product": "Bolu Pandan", "qty": 2}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "full", "va"):
        await handle_message(WA, msg)
    order = await store.get_active_pending(WA)
    assert order.payment_type == "full" and order.amount_due == 150000


async def test_qris_channel_returns_qr_link():
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    for msg in ("sudah sesuai", "Budi", "Jl. Test 1", "pickup", "ya", "full"):
        await handle_message(WA, msg)
    r = await handle_message(WA, "qris")
    assert "https://api.qr/mid-test" in r.text   # QR link relayed to customer
    assert "8808" not in r.text


async def test_identity_validation_rejects_bad_input():
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    await handle_message(WA, "sudah sesuai")
    await handle_message(WA, "Budi")
    await handle_message(WA, "Jl. Test 1")
    await handle_message(WA, "delivery")
    r = await handle_message(WA, "12")
    assert "valid" in r.text.lower()
    assert "nomor_hp" not in json.loads((await store.get_or_create_session(WA)).customer_json)


async def test_cancel_during_confirmation():
    await _seed_cart_awaiting_confirmation([{"product": "Brownies Coklat", "qty": 1}])
    await handle_message(WA, "batal")
    assert (await store.get_or_create_session(WA)).state == State.IDLE
    assert await store.get_cart(WA) == []


async def test_single_active_order_guard():
    from app.tools.add_to_cart import add_to_cart
    await store.create_pending_order(
        wa_number=WA, order_ref="100", payment_type="full", total_amount=100, amount_due=100,
        items_json="[]", customer_json="{}", delivery_method="pickup",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )
    set_turn_context(TurnContext(wa_number=WA))
    out = await add_to_cart.ainvoke({"items": [{"product": "Brownies Coklat", "qty": 1}]})
    assert "website" in out.lower()


# ── get_order_status (backend) / cancel_order (backend) tools ─────────────────
async def test_order_status_reads_backend(patch_externals):
    from app.tools.order_status import get_order_status

    async def latest(wa):
        return {"id": 9, "status": "in_process", "total_harga_pesanan": 100000,
                "invoice": {"nomor_invoice": "INV-9", "status": "partial"},
                "items": [{"product_id": 5, "jumlah": 2}]}
    patch_externals["monkeypatch"].setattr(patch_externals["backend"], "get_latest_order", latest)

    set_turn_context(TurnContext(wa_number=WA))
    out = await get_order_status.ainvoke({})
    assert "INV-9" in out and "diproses" in out.lower()


async def test_cancel_calls_backend():
    from app.tools.cancel_order import cancel_order
    await store.create_pending_order(
        wa_number=WA, order_ref="55", payment_type="full", total_amount=100, amount_due=100,
        items_json="[]", customer_json="{}", delivery_method="pickup",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )
    set_turn_context(TurnContext(wa_number=WA))
    out = await cancel_order.ainvoke({})
    assert "dibatalkan" in out.lower()
    assert await store.get_active_pending(WA) is None


# ── Human takeover suppresses auto-reply ──────────────────────────────────────
async def test_escalate_sets_takeover_and_suppresses(patch_externals):
    from app.tools.escalate import escalate_to_admin
    set_turn_context(TurnContext(wa_number=WA))
    await escalate_to_admin.ainvoke({"reason": "kue custom ulang tahun"})
    assert await store.is_takeover_active(WA) is True
    assert any("628999000111" == wa for wa, _ in patch_externals["sent"])
    reply = await handle_message(WA, "halo?")
    assert reply.suppressed is True


# ── Owner reports (real backend data; honest fallback while endpoint absent) ──
async def test_reports_owner_gating_and_real_data(patch_externals):
    from app.tools.reports import business_analytics, financial_report

    async def summary(start, end):
        return {"revenue": 500000, "expenses": 200000, "order_count": 3,
                "avg_order_value": 166667,
                "top_products": [{"nama_produk": "Brownies Coklat", "qty": 4, "revenue": 200000}]}
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_report_summary", summary)

    set_turn_context(TurnContext(wa_number=WA))          # not an owner
    assert "hanya untuk owner" in (await financial_report.ainvoke({})).lower()

    set_turn_context(TurnContext(wa_number="628777000222@c.us"))  # OWNER_WA_NUMBERS
    fin = await financial_report.ainvoke({})
    assert "Rp500.000" in fin and "Rp300.000" in fin     # revenue & profit
    ana = await business_analytics.ainvoke({})
    assert "Brownies Coklat" in ana and "Rp166.667" in ana


async def test_reports_unavailable_when_endpoint_missing(patch_externals):
    from app.tools.reports import financial_report

    async def none_summary(start, end):
        return None
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_report_summary", none_summary)
    set_turn_context(TurnContext(wa_number="628777000222@c.us"))
    out = await financial_report.ainvoke({})
    assert "belum" in out.lower() and "dummy" not in out.lower()


async def test_takeover_ended_on_admin_site_unsuppresses(patch_externals):
    """Admin deactivates takeover via Admin Site (backend) — the chatbot's local
    cache must yield to the backend and resume replying."""
    await store.activate_takeover(WA)

    async def backend_says_inactive(wa):
        return {"nomor_wa": wa, "human_takeover_active": False, "is_expired": False}
    patch_externals["monkeypatch"].setattr(
        patch_externals["backend"], "get_takeover_status", backend_says_inactive)

    from app.llm import agent as agent_mod
    async def fake_agent(wa, text, history):
        return "Halo! Ada yang bisa kubantu?"
    patch_externals["monkeypatch"].setattr(
        "app.conversation.orchestrator.run_agent", fake_agent)

    reply = await handle_message(WA, "halo?")
    assert reply.suppressed is False                     # bot talks again
    assert await store.is_takeover_active(WA) is False   # local cache synced


# ── Background worker: timeout + paid detection ───────────────────────────────
async def test_background_timeout_cancels(patch_externals):
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    await store.create_pending_order(
        wa_number=WA, order_ref="200", payment_type="full", total_amount=100, amount_due=100,
        items_json="[]", customer_json="{}", delivery_method="pickup", expires_at=past,
    )
    await background._check_once()
    assert await store.get_active_pending(WA) is None
    assert any("dibatalkan otomatis" in t for _, t in patch_externals["sent"])


async def test_background_detects_paid(patch_externals):
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    await store.create_pending_order(
        wa_number=WA, order_ref="201", payment_type="full", total_amount=100, amount_due=100,
        items_json="[]", customer_json="{}", delivery_method="pickup", expires_at=future,
    )

    async def paid(order_id):
        return {"invoice_status": "paid", "amount_paid": 100, "amount_due": 0}
    patch_externals["monkeypatch"].setattr(patch_externals["backend"], "get_payment_status", paid)

    await background._check_once()
    order = await store.get_active_pending(WA)
    assert order.status == "paid"
    assert (await store.get_or_create_session(WA)).state == State.ORDER_ACTIVE
    assert any("sudah kami terima" in t for _, t in patch_externals["sent"])


async def test_c4_ready_push_matches_by_order_ref(patch_externals):
    """Backend pushes the BACKEND order id; notify_ready must match by order_ref."""
    await store.create_pending_order(
        wa_number=WA, order_ref="777", payment_type="full", total_amount=100, amount_due=100,
        items_json="[]", customer_json="{}", delivery_method="delivery",
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30),
    )
    await store.update_pending_order((await store.get_active_pending(WA)).id, status="paid")
    assert await background.notify_ready(777) is True          # backend order id
    assert any("siap" in t.lower() for _, t in patch_externals["sent"])
