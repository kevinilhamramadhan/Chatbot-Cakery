"""Checkout finalization: real customer+order+payment via the main backend.

Order/invoice/payment live in the backend (Neon + Midtrans). We keep a local
`pending_orders` row (order_ref = backend order_id) only for timeout tracking,
the single-active-order guard, and payment polling (PROMPT §10.8-10).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx

from app.backend_client import api as backend
from app.backend_client import products as products_api
from app.conversation import bahasa, store
from app.conversation.context import OutboundMedia, get_turn_context_or_none
from app.conversation.states import State
from app.core.config import settings
from app.tools.formatting import rupiah

logger = logging.getLogger(__name__)


def cart_total(cart: list[dict]) -> float:
    return sum(float(i["harga"]) * int(i["qty"]) for i in cart)


def _alasan_backend(exc: httpx.HTTPStatusError) -> str:
    """Kalimat penolakan dari backend yang layak diteruskan apa adanya.

    Hanya untuk 400 dengan `detail` berupa satu kalimat — itu pesan bisnis yang
    memang ditulis untuk dibaca orang ("Stok produk 'X' sedang habis."). Bentuk
    lain (daftar galat validasi, HTML, jejak galat) tidak diteruskan supaya
    pelanggan tidak menerima isi perut sistem.
    """
    res = exc.response
    if res is None or res.status_code != 400:
        return ""
    try:
        detail = res.json().get("detail")
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(detail, str):
        return ""
    detail = detail.strip()
    return detail if 0 < len(detail) <= 200 else ""


async def reprice_cart(cart: list[dict], lang: str | None = None) -> tuple[list[dict], list[str]]:
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
            notes.append(bahasa.teks("item_dikeluarkan", lang, nama=item["nama"]))
            continue
        harga = p.get("harga_jual")
        if harga is None:
            notes.append(bahasa.teks("item_dikeluarkan", lang, nama=item["nama"]))
            continue
        harga = float(harga)
        if harga != float(item["harga"]):
            notes.append(bahasa.teks("harga_berubah", lang, nama=item["nama"],
                                     lama=rupiah(item["harga"]), baru=rupiah(harga)))
        fresh.append({**item, "harga": harga})
    return fresh, notes


def kirim_gambar_qris(qris_url: str, lang: str) -> None:
    """Titipkan gambar QR ke giliran yang sedang berjalan.

    Diam saja kalau dipanggil di luar giliran (mis. dari tugas latar): kabar
    proaktif tidak punya tempat menitipkan lampiran, dan teksnya sudah cukup.
    """
    ctx = get_turn_context_or_none()
    if ctx is None:
        return
    caption = bahasa.teks("kapsi_qris", lang)
    # Mode sandbox: tautan gambarnya ikut ditulis supaya bisa ditempel ke
    # simulator pembayaran Midtrans (simulator menerima URL gambar QR, bukan
    # foto layar). Tagihan sungguhan tidak memuat "sandbox" di URL-nya, jadi
    # pelanggan asli tetap hanya menerima gambar.
    if "sandbox" in qris_url:
        caption += "\n\n" + bahasa.teks("tautan_qris_uji", lang, url=qris_url)
    ctx.media.append(OutboundMedia(image_url=qris_url, caption=caption))


async def finalize_order(wa_number: str) -> str:
    cart = await store.get_cart(wa_number)
    cust = await store.get_customer(wa_number)
    lang = await store.get_lang(wa_number)
    if not cart:
        await store.set_state(wa_number, State.IDLE)
        return bahasa.teks("keranjang_kosong", lang)

    # Never charge from a stale snapshot: re-read prices/availability now.
    try:
        cart, price_notes = await reprice_cart(cart, lang)
    except Exception as exc:  # noqa: BLE001 - backend hiccup: don't guess a price
        logger.exception("reprice failed: %s", exc)
        return bahasa.teks("harga_belum_pasti", lang)
    if not cart:
        await store.set_cart(wa_number, [])
        await store.set_state(wa_number, State.IDLE)
        return bahasa.teks("semua_item_habis", lang)
    if price_notes:
        # Changed total = a new offer; the customer must re-confirm it.
        await store.set_cart(wa_number, cart)
        await store.set_state(wa_number, State.AWAITING_CART_CONFIRMATION)
        return bahasa.teks("ada_update_harga", lang, catatan="; ".join(price_notes),
                           total=rupiah(cart_total(cart)))

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
    except httpx.HTTPStatusError as exc:
        # 409 is a business answer, not a hiccup: the backend refuses a second
        # order while an invoice is still unpaid. Telling the customer to "coba
        # ulangi sebentar lagi" sent them into a retry that can never succeed.
        if exc.response is not None and exc.response.status_code == 409:
            logger.info("backend refused a second order for this customer")
            await store.set_state(wa_number, State.IDLE)
            return bahasa.teks("masih_ada_tagihan", lang)
        # 400 juga jawaban bisnis, bukan gangguan sesaat: backend menolak
        # pesanan yang stok bahannya tidak cukup. Menyuruh "coba ulangi sebentar
        # lagi" mengirim pelanggan ke pengulangan yang tidak akan pernah
        # berhasil, dan alasannya sudah ada di badan balasannya.
        alasan = _alasan_backend(exc)
        if alasan:
            logger.info("backend refused the order: %s", alasan)
            await store.set_state(wa_number, State.IDLE)
            await store.set_cart(wa_number, [])
            return bahasa.teks("pesanan_ditolak", lang,
                               alasan=f"{alasan[0].lower()}{alasan[1:]}")
        logger.exception("create order failed: %s", exc)
        return bahasa.teks("pesanan_gagal", lang)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create order failed: %s", exc)
        return bahasa.teks("pesanan_gagal", lang)

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
        return bahasa.teks("tagihan_gagal", lang)

    # An invoice nobody can pay is worse than an honest failure. The backend
    # answers 201 even when Midtrans rejects the charge (a second charge on the
    # same order comes back 406, with pg_transaction_id and qris_url both null),
    # and the checkout message was still sent — with an empty payment line and a
    # 30-minute deadline under it.
    va = pay.get("va_number")
    qris = pay.get("qris_url")
    if not va and not qris:
        logger.error("charge for order %s returned no payment instrument: %s",
                     order_id, {k: pay.get(k) for k in ("status", "pg_transaction_id")})
        try:
            await backend.cancel_order(order_id)
        except Exception:  # noqa: BLE001
            logger.warning("could not cancel unpayable order %s", order_id)
        return bahasa.teks("tagihan_tanpa_cara_bayar", lang)

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.payment_timeout_minutes)
    # QRIS dikirim sebagai GAMBAR, bukan tautan. Pelanggan di WhatsApp tidak bisa
    # men-scan URL: dia harus membukanya di browser lalu memotretnya dengan HP
    # lain. Tautannya tetap disimpan di kolom qris_url supaya bisa dikirim ulang
    # (resend_payment_method) tanpa menerbitkan tagihan baru.
    pay_line = (f"💳 Virtual Account: *{va}*" if va
                else bahasa.teks("scan_qris", lang))

    # 3) Track locally (order_ref = backend order_id) for timeout/poll/guard.
    # The invoice number and the payment line are snapshotted here so every
    # later message can name the same invoice and re-send the same instructions.
    await store.create_pending_order(
        wa_number=wa_number,
        order_ref=str(order_id),
        payment_ref=pay.get("pg_transaction_id"),
        payment_type=payment_type,
        total_amount=total,
        amount_due=amount_due,
        nomor_invoice=nomor_invoice,
        pay_instruction=pay_line,
        qris_url=None if va else qris,
        items_json=json.dumps(cart, ensure_ascii=False),
        customer_json=json.dumps(cust, ensure_ascii=False),
        delivery_method=delivery,
        expires_at=expires_at,
    )
    await store.set_cart(wa_number, [])
    await store.set_state(wa_number, State.AWAITING_PAYMENT)

    if not va and qris:
        kirim_gambar_qris(qris, lang)
    return bahasa.teks(
        "pesanan_dibuat", lang,
        invoice=nomor_invoice,
        label=bahasa.teks("label_bayar_penuh" if payment_type == "full" else "label_dp", lang),
        jumlah=rupiah(amount_due),
        total=(bahasa.teks("total_pesanan_dp", lang, total=rupiah(total))
               if payment_type == "dp" else ""),
        cara_bayar=pay_line,
        menit=settings.payment_timeout_minutes,
    )
