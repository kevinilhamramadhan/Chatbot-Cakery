"""Tool: add_to_cart — builds the local draft order and asks for confirmation.

The draft cart lives locally in the session; checkout finalizes it into a real
backend order + Midtrans charge (PROMPT §9, §10.4).
"""

from langchain_core.tools import tool

from app.conversation import store
from app.conversation.context import get_turn_context
from app.conversation.states import State
from app.tools.formatting import options_line, product_label, resolve_product, rupiah


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
    for raw in items:
        name_q = str(raw.get("product") or raw.get("nama") or "").strip()
        try:
            # ponytail: hard cap 100/item guards against LLM/typo garbage qty
            # creating absurd real orders; raise if the store ever needs more.
            qty = min(max(1, int(raw.get("qty", 1))), 100)
        except (TypeError, ValueError):
            qty = 1
        if not name_q:
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
        return f"Maaf, aku tidak menemukan {nf} di menu. Coba cek menu dulu ya."

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
    msg += "\n\nSudah sesuai semua, atau mau nambah lagi? Ketik *sudah sesuai* untuk lanjut ya 😊"
    return msg
