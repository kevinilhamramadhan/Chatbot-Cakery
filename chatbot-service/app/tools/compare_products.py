"""Tool: compare_products — REAL (client-side logic). No images sent (PROMPT §10.3)."""

from langchain_core.tools import tool

from app.tools.formatting import menu_fallback, options_line, product_label, resolve_product, rupiah


@tool
async def compare_products(products: list[str]) -> str:
    """Bandingkan 2 produk atau lebih (nama/id) berdasarkan harga & deskripsi.

    Jangan kirim foto. Gunakan saat pelanggan minta membandingkan beberapa kue.
    """
    if not products or len(products) < 2:
        return "Sebutkan minimal 2 produk yang ingin dibandingkan ya."

    resolved = []
    not_found = []
    for q in products:
        p, options = await resolve_product(q)
        if p:
            resolved.append(p)
        elif options:
            not_found.append(f"{q} (ambigu — maksudnya: {options_line(options, 3)}?)")
        else:
            not_found.append(q)

    if len(resolved) < 2:
        nf = ", ".join(not_found)
        return await menu_fallback(f"Maaf, aku tidak menemukan: {nf}.")

    lines = ["Perbandingan produk:"]
    for p in resolved:
        desc = (p.get("deskripsi") or "-")
        if len(desc) > 100:
            desc = desc[:100] + "…"
        lines.append(f"\n*{product_label(p)}*\n  Harga: {rupiah(p.get('harga_jual'))}\n  {desc}")
    if not_found:
        lines.append(f"\n(Tidak ditemukan: {', '.join(not_found)})")
    return "\n".join(lines)
