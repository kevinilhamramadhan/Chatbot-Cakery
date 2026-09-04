"""Tool: get_menu — REAL, hits the backend product list."""

from langchain_core.tools import tool

from app.backend_client import products as products_api
from app.tools.formatting import product_label, rupiah


def _matches(product: dict, needle: str) -> bool:
    haystack = " ".join(
        str(product.get(key) or "")
        for key in ("kategori", "parent_category", "nama_produk")
    )
    return needle in haystack.casefold()


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
    if kategori and kategori.strip():
        needle = kategori.strip().casefold()
        matched = [p for p in items if _matches(p, needle)]
        if matched:
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
    lines.append("\nMau lihat detail salah satu kue? Sebutkan namanya ya 😊")
    return "\n".join(lines)
