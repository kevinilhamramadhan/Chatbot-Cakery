"""Checkout finalization: real customer+order+payment via the main backend.

Order/invoice/payment live in the backend (Neon + Midtrans). We keep a local
`pending_orders` row (order_ref = backend order_id) only for timeout tracking,
the single-active-order guard, and payment polling (PROMPT §10.8-10).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from app.backend_client import api as backend
from app.backend_client import products as products_api
from app.conversation import store
from app.conversation.states import State
from app.core.config import settings
from app.tools.formatting import rupiah

logger = logging.getLogger(__name__)


def cart_total(cart: list[dict]) -> float:
    return sum(float(i["harga"]) * int(i["qty"]) for i in cart)


async def reprice_cart(cart: list[dict]) -> tuple[list[dict], list[str]]:
    """Refresh every line against the live backend price just before charging.

    Prices are snapshotted at add_to_cart time and a draft cart can sit in the
    session indefinitely, so the amount we ask the backend to charge could be
    based on a price that no longer exists (or on a product since disabled).
    Returns (repriced_cart, notes_for_customer).
    """
    notes: list[str] = []
    fresh: list[dict] = []
    for item in cart:
        p = await products_api.get_product(int(item["product_id"]))
        if p is None or not p.get("is_available", True):
            notes.append(f"{item['nama']} sudah tidak tersedia dan aku keluarkan dari pesanan")
            continue
        harga = p.get("harga_jual")
        if harga is None:
            notes.append(f"{item['nama']} sudah tidak tersedia dan aku keluarkan dari pesanan")
            continue
        harga = float(harga)
        if harga != float(item["harga"]):
            notes.append(
                f"harga {item['nama']} berubah dari {rupiah(item['harga'])} "
                f"jadi {rupiah(harga)}"
            )
        fresh.append({**item, "harga": harga})
    return fresh, notes


async def finalize_order(wa_number: str) -> str:
    cart = await store.get_cart(wa_number)
    cust = await store.get_customer(wa_number)
    if not cart:
        await store.set_state(wa_number, State.IDLE)
        return "Keranjangmu kosong. Mau lihat menu dulu?"

    # Never charge from a stale snapshot: re-read prices/availability now.
    try:
        cart, price_notes = await reprice_cart(cart)
    except Exception as exc:  # noqa: BLE001 - backend hiccup: don't guess a price
        logger.exception("reprice failed: %s", exc)
        return "Maaf, harga tidak bisa dipastikan sekarang. Coba ulangi sebentar lagi ya 🙏"
    if not cart:
        await store.set_cart(wa_number, [])
        await store.set_state(wa_number, State.IDLE)
        return (
            "Maaf, semua item di pesananmu sudah tidak tersedia. "
            "Mau lihat menu terbaru?"
        )
    if price_notes:
        # Changed total = a new offer; the customer must re-confirm it.
        await store.set_cart(wa_number, cart)
        await store.set_state(wa_number, State.AWAITING_CART_CONFIRMATION)
        return (
            "Sebelum lanjut, ada update: " + "; ".join(price_notes) + ".\n\n"
            + f"Total sekarang: {rupiah(cart_total(cart))}\n"
            "Ketik *sudah sesuai* kalau setuju, atau *batal* untuk membatalkan ya 🙏"
        )

    total = cart_total(cart)
    payment_type = cust.get("payment_type", "full")
    if payment_type == "dp" and settings.allow_down_payment:
        # 2 decimals, not whole rupiah: the backend validates `amount` against
        # its own (total * 0.5).quantize(0.01) and 400s on any mismatch, so an
        # odd total must round the same way on both sides.
        amount_due = round(total * settings.down_payment_percentage, 2)
    else:
        payment_type = "full"
        amount_due = total
    delivery = cust.get("metode_pengiriman", "pickup")

    # 1) Customer + order -> real backend (Neon).
    try:
        customer = await backend.upsert_customer(
            wa_number, cust.get("nama", ""), cust.get("alamat", ""), cust.get("nomor_hp", "")
        )
        order = await backend.create_order(
            customer_id=customer["customer_id"],
            items=[{"product_id": c["product_id"], "jumlah": c["qty"]} for c in cart],
            metode_pengiriman=delivery,
            created_via="chatbot",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("create order failed: %s", exc)
        return "Maaf, pembuatan pesanan gagal. Coba ulangi sebentar lagi ya 🙏"

    order_id = order["order_id"]
    nomor_invoice = order.get("nomor_invoice") or f"#{order_id}"

    # 2) Charge via backend -> Midtrans (VA/QRIS per customer choice at step 6).
    channel = cust.get("channel", "bank_transfer")
    try:
        pay = await backend.create_payment(order_id, amount_due, channel=channel,
                                           payment_type=payment_type)
    except Exception as exc:  # noqa: BLE001
        logger.exception("payment charge failed: %s", exc)
        # Cancel the just-created backend order so a retry doesn't stack
        # duplicate pending orders in the backend DB. Best-effort.
        try:
            await backend.cancel_order(order_id)
        except Exception:  # noqa: BLE001
            logger.warning("could not cancel orphaned order %s", order_id)
        return "Maaf, pembuatan tagihan gagal. Coba ulangi sebentar lagi ya 🙏"

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.payment_timeout_minutes)

    # 3) Track locally (order_ref = backend order_id) for timeout/poll/guard.
    await store.create_pending_order(
        wa_number=wa_number,
        order_ref=str(order_id),
        payment_ref=pay.get("pg_transaction_id"),
        payment_type=payment_type,
        total_amount=total,
        amount_due=amount_due,
        items_json=json.dumps(cart, ensure_ascii=False),
        customer_json=json.dumps(cust, ensure_ascii=False),
        delivery_method=delivery,
        expires_at=expires_at,
    )
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.AWAITING_PAYMENT)

    paid_label = "Pembayaran penuh" if payment_type == "full" else "DP 50%"
    va = pay.get("va_number")
    qris = pay.get("qris_url")
    pay_line = f"💳 Virtual Account: *{va}*" if va else (f"Scan QRIS: {qris}" if qris else "")
    return (
        f"Pesanan kamu sudah dibuat ✅\nNo. Invoice: *{nomor_invoice}*\n\n"
        f"{paid_label} yang harus dibayar: *{rupiah(amount_due)}*"
        + (f" (total pesanan {rupiah(total)})" if payment_type == "dp" else "")
        + f"\n\n{pay_line}\n\n"
        f"Batas waktu pembayaran: {settings.payment_timeout_minutes} menit. "
        "Pembayaran akan terdeteksi otomatis. Ketik *batal* kalau ingin membatalkan."
    )
