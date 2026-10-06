"""Tool: update_cart — mengubah jumlah atau menghapus item yang SUDAH ada di keranjang.

Sebelum tool ini ada, "brownies nya jadi 3" dijawab add_to_cart untuk produk
lain di katalog ("brownies" -> Brownies Coklat), dan "hapus lapis legitnya"
dijawab cancel_order (terukur 6 Okt 2026). Tidak ada tool yang bisa mengurangi
isi keranjang, jadi pelanggan yang salah jumlah harus membatalkan semuanya.

`qty` adalah jumlah AKHIR, bukan selisih: 0 berarti item itu dibuang. Nama
dicocokkan ke isi keranjang saja, bukan ke katalog — yang diubah pasti sesuatu
yang sudah dipilih pelanggan.
"""

import re

from langchain_core.tools import tool

from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.conversation.states import State, mentions_quantity
from app.core.config import settings
from app.tools.add_to_cart import _as_items, cart_summary

_KATA = re.compile(r"[a-z0-9]+")


def _cocok_di_keranjang(nama: str, cart: list[dict]) -> tuple[dict | None, list[dict]]:
    """(item, []) kalau satu item paling cocok; (None, [a, b]) kalau seri;
    (None, []) kalau tidak ada yang cocok."""
    q = set(_KATA.findall(nama.lower()))
    if not q:
        return None, []
    skor = [(len(q & set(_KATA.findall(str(it["nama"]).lower()))), it) for it in cart]
    terbaik = max((s for s, _ in skor), default=0)
    if terbaik == 0:
        return None, []
    kandidat = [it for s, it in skor if s == terbaik]
    return (kandidat[0], []) if len(kandidat) == 1 else (None, kandidat)


def _jumlah_akhir(raw) -> int | None:
    """Bilangan bulat >= 0, atau None = "tanya, jangan menebak"."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, float) and not raw.is_integer():
        return None
    try:
        qty = int(raw)
    except (TypeError, ValueError):
        return None
    return qty if qty >= 0 else None


@tool
async def update_cart(items: list[dict] | None = None, product: str | None = None,
                      qty: int | None = None) -> str:
    """Ubah jumlah atau hapus item yang SUDAH ada di keranjang pelanggan.

    `items` adalah list objek berisi `product` (nama kue di keranjang) dan `qty`
    (jumlah AKHIR yang diinginkan; 0 = hapus item itu dari keranjang).
    Contoh: [{"product": "Brownies Coklat", "qty": 3}] atau
    [{"product": "Bolu Pandan", "qty": 0}].
    Gunakan saat pelanggan mengganti jumlah ("jadi 3", "ganti jadi 1") atau
    membuang satu kue dari keranjang ("hapus", "ga jadi yang itu"). Untuk
    MENAMBAH kue ke keranjang pakai `add_to_cart`.
    """
    items = _as_items(items, product=product, qty=qty)
    ctx = get_turn_context()
    wa = ctx.wa_number
    lang = await store.get_lang(wa)

    if await store.get_active_pending(wa) is not None:
        return bahasa.teks("ubah_keranjang_sudah_ditagih", lang)
    cart = await store.get_cart(wa)
    if not cart:
        return bahasa.teks("keranjang_kosong_tool", lang)
    if not items:
        return bahasa.teks("ubah_keranjang_item_mana", lang) + "\n\n" + cart_summary(cart, lang)

    tak_ada, seri, tak_jelas, minimum, besar = [], [], [], [], []
    berubah = False
    for raw in items:
        nama = str(raw.get("product") or raw.get("nama") or "").strip()
        if not nama:
            continue
        item, kandidat = _cocok_di_keranjang(nama, cart)
        if item is None:
            (seri if kandidat else tak_ada).append(nama)
            continue
        jml = _jumlah_akhir(raw.get("qty"))
        # Jumlah selain 0 harus angka yang ditulis pelanggan sendiri — aturan
        # yang sama dengan add_to_cart: model tidak boleh mengarang jumlah.
        if jml is None or (jml > 0 and ctx.user_text and not mentions_quantity(ctx.user_text)):
            tak_jelas.append(item["nama"])
            continue
        if jml == 0:
            cart.remove(item)
            berubah = True
            continue
        if jml > settings.max_self_service_qty:
            besar.append(f"{item['nama']} x{jml}")
            continue
        min_order = await _minimum(item)
        if jml < min_order:
            minimum.append(bahasa.teks("ubah_keranjang_minimum_item", lang,
                                       nama=item["nama"], minimum=min_order))
            continue
        if item["qty"] != jml:
            item["qty"] = jml
            berubah = True

    if berubah:
        await store.set_cart(wa, cart)
    if not cart:
        ctx.next_state = State.IDLE
        return bahasa.teks("keranjang_jadi_kosong", lang)

    catatan = []
    if tak_ada:
        catatan.append(bahasa.teks("ubah_keranjang_tak_ada", lang, nama=", ".join(tak_ada)))
    if seri:
        catatan.append(bahasa.teks("ubah_keranjang_seri", lang, nama=", ".join(seri)))
    if tak_jelas:
        catatan.append(bahasa.teks("ubah_keranjang_jumlah", lang, nama=", ".join(tak_jelas)))
    if minimum:
        catatan.append("; ".join(minimum))
    if besar:
        catatan.append(bahasa.teks("ubah_keranjang_besar", lang, nama="; ".join(besar)))

    ctx.next_state = State.AWAITING_CART_CONFIRMATION
    msg = cart_summary(cart, lang)
    if catatan:
        msg += "\n\n(" + " ".join(catatan) + ")"
    return msg + bahasa.teks("keranjang_tanya_tambah", lang)


async def _minimum(item: dict) -> int:
    from app.backend_client import products as products_api

    try:
        p = await products_api.get_product(item.get("product_id"))
    except Exception:  # noqa: BLE001 - katalog tak terbaca: jangan menghalangi perubahan
        return 1
    return max(1, int((p or {}).get("minimum_order") or 1))
