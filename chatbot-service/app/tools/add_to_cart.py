"""Tool: add_to_cart — builds the local draft order and asks for confirmation.

The draft cart lives locally in the session; checkout finalizes it into a real
backend order + Midtrans charge (PROMPT §9, §10.4).
"""

from langchain_core.tools import tool

from app.conversation import store
from app.conversation.context import get_turn_context
from app.conversation.states import State, mentions_quantity
from app.core.config import settings
from app.tools.formatting import (
    menu_fallback,
    options_line,
    product_label,
    resolve_product,
    rupiah,
)


def _parse_qty(raw) -> int | None:
    """A whole positive number, or None meaning "ask, don't guess".

    The old `min(max(1, int(qty)), 100)` rewrote every bad quantity in silence:
    1000 became 100 (an Rp8.5jt cart nobody asked for), -5 became +1 on top of
    what was already there, and 0.5 from "setengah" became 1.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, float) and not raw.is_integer():
        return None
    try:
        qty = int(raw)
    except (TypeError, ValueError):
        return None
    return qty if qty > 0 else None


def cart_summary(cart: list[dict]) -> str:
    if not cart:
        return "Keranjang masih kosong."
    lines = ["Ringkasan pesananmu sejauh ini:"]
    total = 0.0
    for it in cart:
        sub = float(it["harga"]) * int(it["qty"])
        total += sub
        lines.append(f"• {it['nama']} x{it['qty']} = {rupiah(sub)}")
    lines.append(f"\nTotal: {rupiah(total)}")
    return "\n".join(lines)


@tool
async def add_to_cart(items: list[dict]) -> str:
    """Tambahkan item ke draft pesanan pelanggan.

    `items` adalah list objek berisi `product` (nama/id kue) dan `qty` (jumlah).
    Contoh: [{"product": "Brownies Coklat", "qty": 2}].
    Gunakan saat pelanggan menyatakan ingin memesan kue tertentu dengan jumlahnya.
    """
    ctx = get_turn_context()
    wa = ctx.wa_number

    # PROMPT §10.11 — one active transaction per WA number.
    active = await store.get_active_pending(wa)
    if active is not None:
        return (
            "Kamu masih punya pesanan yang sedang diproses/belum dibayar. "
            "Untuk pesanan baru, silakan selesaikan dulu yang ini atau pesan lewat "
            "website Toti Cakery ya 🙏"
        )

    cart = await store.get_cart(wa)
    added, not_found, unavailable, ambiguous, below_min = [], [], [], [], []
    bulk, unclear_qty = [], []
    for raw in items:
        name_q = str(raw.get("product") or raw.get("nama") or "").strip()
        if not name_q:
            continue
        qty = _parse_qty(raw.get("qty", 1))
        if qty is None:
            unclear_qty.append(name_q)
            continue
        # Defaulting to 1 is fine; anything larger has to be a number the
        # customer actually wrote. "pesan bolu beberapa aja" came back from the
        # model as qty=2 — a quantity nobody asked for — and it was only kept
        # out of the cart because that product happened not to exist.
        if qty > 1 and ctx.user_text and not mentions_quantity(ctx.user_text):
            unclear_qty.append(name_q)
            continue
        p, options = await resolve_product(name_q)
        if p is None and options:
            # Several products fit ("cupcake" -> isi 4/6/9…): ask, never guess.
            ambiguous.append(f"'{name_q}': {options_line(options)}")
            continue
        if p is None:
            not_found.append(name_q)
            continue
        # Backend marks products out of stock via is_available (recipe vs stock).
        if not p.get("is_available", True):
            unavailable.append(product_label(p))
            continue
        harga = p.get("harga_jual")
        if harga is None:
            not_found.append(name_q)
            continue
        # Never quietly shrink a large order into a plausible one — the customer
        # would see a total they never asked for. Hand bulk to a human instead.
        if qty > settings.max_self_service_qty:
            bulk.append(f"{product_label(p)} x{qty}")
            continue
        # Backend exposes products.minimum_order but does NOT enforce it on
        # POST /orders — if we don't check here, the customer gets an invoice
        # for a quantity the store won't bake. Never silently bump the qty:
        # ordering more than asked is worse than asking again.
        min_order = max(1, int(p.get("minimum_order") or 1))
        existing_qty = next(
            (c["qty"] for c in cart if c.get("product_id") == p.get("id")), 0
        )
        if existing_qty + qty < min_order:
            below_min.append(f"{product_label(p)} minimal {min_order} pcs")
            continue
        # Merge with existing line if same product.
        existing = next((c for c in cart if c.get("product_id") == p.get("id")), None)
        if existing:
            # A line already in the cart only grows when the customer wrote a
            # number in THIS message. Without that, "iya udah bener" at the
            # confirmation step came back as add_to_cart for the same item and
            # the order silently doubled — 2 brownies became 4, Rp190.000 became
            # Rp380.000. The guard lives here rather than in the router because
            # it is about the data, not about guessing what the message meant.
            if ctx.user_text and not mentions_quantity(ctx.user_text):
                unclear_qty.append(product_label(p))
                continue
            existing["qty"] += qty
        else:
            cart.append(
                {
                    "product_id": p.get("id"),
                    "nama": product_label(p),
                    "harga": float(harga),
                    "qty": qty,
                }
            )
        added.append(f"{product_label(p)} x{qty}")

    await store.set_cart(wa, cart)

    if not added:
        if bulk and not (not_found or unavailable or ambiguous or below_min or unclear_qty):
            return (
                "Jumlah sebanyak itu (" + "; ".join(bulk) + ") aku teruskan ke admin ya — "
                "pesanan besar perlu dijadwalkan minimal H-2. Mau kusambungkan ke admin, "
                "atau mau kuubah jumlahnya?"
            )
        if unclear_qty and not (not_found or unavailable or ambiguous or below_min):
            return (
                "Jumlahnya belum jelas untuk " + ", ".join(unclear_qty)
                + ". Boleh sebutkan jumlahnya dalam angka utuh, mis. 2? 😊"
            )
        if below_min and not not_found and not unavailable and not ambiguous:
            return (
                "Untuk produk ini ada jumlah minimum pemesanan: "
                + "; ".join(below_min)
                + ". Mau kunaikkan jumlahnya?"
            )
        if ambiguous and not not_found and not unavailable:
            return (
                "Ada beberapa pilihan untuk " + "; ".join(ambiguous)
                + ". Sebutkan yang mana ya? 😊"
            )
        if unavailable and not not_found:
            return (
                f"Maaf, {', '.join(unavailable)} sedang tidak tersedia. "
                "Mau pesan menu yang lain?"
            )
        nf = ", ".join(not_found) if not_found else "item yang diminta"
        return await menu_fallback(f"Maaf, aku tidak menemukan {nf} di menu.")

    # Hand control to the confirmation step.
    ctx.next_state = State.AWAITING_CART_CONFIRMATION

    msg = cart_summary(cart)
    if ambiguous:
        msg += "\n\n(Belum kumasukkan karena ada beberapa pilihan — " + "; ".join(ambiguous) + ")"
    if not_found:
        msg += f"\n\n(Tidak ditemukan: {', '.join(not_found)})"
    if unavailable:
        msg += f"\n\n(Sedang tidak tersedia: {', '.join(unavailable)})"
    if below_min:
        msg += f"\n\n(Belum masuk karena minimum pemesanan: {'; '.join(below_min)})"
    if bulk:
        msg += (f"\n\n(Belum masuk karena jumlahnya besar: {'; '.join(bulk)} — "
                "pesanan sebanyak itu lewat admin ya)")
    if unclear_qty:
        msg += f"\n\n(Jumlahnya belum jelas: {', '.join(unclear_qty)})"
    msg += "\n\nSudah sesuai semua, atau mau nambah lagi? Ketik *sudah sesuai* untuk lanjut ya 😊"
    return msg
