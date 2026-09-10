"""Tool: get_menu — REAL, hits the backend product list."""

from langchain_core.tools import tool

from app.backend_client import products as products_api
from app.conversation.context import get_turn_context_or_none
from app.tools.formatting import product_label, rupiah


def _matches(product: dict, needle: str) -> bool:
    """Match a category needle against the product's CATEGORY only, never its name.

    nama_produk used to be in this haystack, and that quietly shrank the menu:
    the model invents a `kategori` on most menu questions, and an invented
    'cake' matched "Chiffon Cake Pandan" by name — so the customer was shown one
    product out of three with nothing saying the list had been filtered.
    Measured on the live catalogue: 'cake' 1/3, 'kue' 1/3, 'premium' 1/3.
    """
    haystack = " ".join(
        str(product.get(key) or "") for key in ("kategori", "parent_category")
    )
    return needle in haystack.casefold()


def _customer_asked_for(kategori: str) -> bool:
    """Only filter by a category the customer actually typed.

    The model attaches `kategori='cake'` to plain "menu dong" more often than
    not. That was harmless while no product carried the category "cake" — the
    filter matched nothing and the whole menu came back. Once the real catalogue
    arrived, "cake" became a genuine category, and a bare "menu dong" started
    answering with 14 of 23 products. Same rule as quantities: an argument the
    customer never wrote is an argument the model made up.
    """
    ctx = get_turn_context_or_none()
    if ctx is None or not ctx.user_text:
        return True  # called outside a turn (tests, scripts): trust the caller
    return kategori.strip().casefold() in ctx.user_text.casefold()


@tool
async def get_menu(kategori: str | None = None) -> str:
    """Ambil daftar menu/produk aktif Toti Cakery beserta harganya.

    Gunakan saat pelanggan menanyakan menu, daftar kue, atau harga secara umum.
    Parameter `kategori` opsional untuk memfilter (mis. 'cake', 'pastry').
    """
    # Filtering happens here, not in the query: the backend matches `kategori`
    # exactly and case-sensitively, while the model invents categories the
    # catalogue never uses ("cake", "cookies", "kue"). Every one of those came
    # back empty and the customer was told the menu was unavailable — a fake
    # outage in answer to "ada kue ultah gak?".
    items = await products_api.list_products(only_active=True)
    if not items:
        return "Maaf, daftar menu sedang tidak bisa diambil. Coba lagi sebentar lagi ya."

    heading = "Berikut menu Toti Cakery:"
    filtered = False
    if kategori and kategori.strip() and _customer_asked_for(kategori):
        needle = kategori.strip().casefold()
        matched = [p for p in items if _matches(p, needle)]
        if matched and len(matched) < len(items):
            items = matched
            filtered = True
        elif matched:
            items = matched
        else:
            # Keep the "Berikut menu" prefix: agent._history_view() keys off it
            # to replace this whole block with a marker, and a menu that slips
            # back into the context verbatim is what taught the model to answer
            # menu questions from memory in the first place.
            heading = (f"Berikut menu Toti Cakery (aku belum menemukan kategori "
                       f"'{kategori.strip()}', jadi ini semuanya ya):")

    lines = [heading]
    for p in items:
        # is_available is computed by the backend (recipe vs stock). Absent -> available.
        status = "" if p.get("is_available", True) else "  (sedang tidak tersedia)"
        lines.append(f"• {product_label(p)} — {rupiah(p.get('harga_jual'))}{status}")
    if filtered:
        # A filtered list must say so. Silently showing a subset is how the
        # customer ends up believing the shop sells one cake.
        lines.append(f"\n(ini yang cocok dengan '{kategori.strip()}' — "
                     "ketik *menu* untuk daftar lengkapnya)")
    lines.append("\nMau lihat detail salah satu kue? Sebutkan namanya ya 😊")
    return "\n".join(lines)
