"""Tool: compare_products — REAL (client-side logic). No images sent (PROMPT §10.3)."""

from langchain_core.tools import tool

from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.tools.formatting import menu_fallback, options_line, product_label, resolve_product, rupiah


@tool
async def compare_products(products: list[str]) -> str:
    """Bandingkan 2 produk atau lebih (nama/id) berdasarkan harga & deskripsi.

    Jangan kirim foto. Gunakan saat pelanggan minta membandingkan beberapa kue.
    """
    lang = await store.get_lang(get_turn_context().wa_number)
    if not products or len(products) < 2:
        return bahasa.teks("banding_minimal", lang)

    resolved = []
    not_found = []
    for q in products:
        p, options = await resolve_product(q)
        if p:
            resolved.append(p)
        elif options:
            not_found.append(bahasa.teks("banding_ambigu", lang, q=q,
                                         pilihan=options_line(options, 3)))
        else:
            not_found.append(q)

    if len(resolved) < 2:
        nf = ", ".join(not_found)
        return await menu_fallback(bahasa.teks("tidak_menemukan", lang, nama=nf), lang)

    lines = [bahasa.teks("banding_judul", lang)]
    for p in resolved:
        desc = (p.get("deskripsi") or "-")
        if len(desc) > 100:
            desc = desc[:100] + "…"
        lines.append(bahasa.teks("banding_baris", lang, nama=product_label(p),
                                 harga=rupiah(p.get("harga_jual")), deskripsi=desc))
    if not_found:
        lines.append(bahasa.teks("catatan_tidak_ditemukan", lang,
                                 nama=", ".join(not_found))[1:])
    return "\n".join(lines)
