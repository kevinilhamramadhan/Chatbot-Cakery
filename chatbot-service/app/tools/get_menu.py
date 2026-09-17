"""Tool: get_menu — REAL, hits the backend product list."""

from langchain_core.tools import tool

from app.backend_client import products as products_api
from app.conversation import bahasa, store
from app.conversation.context import get_turn_context_or_none
from app.core.config import settings
from app.tools.formatting import product_label, rupiah, tersedia

# Kategori tanpa nama di data ditaruh paling bawah, bukan di bawah judul kosong.
_TANPA_KATEGORI = "Lainnya"


def _kategori(product: dict) -> str:
    for key in ("kategori", "parent_category"):
        nilai = str(product.get(key) or "").strip()
        if nilai:
            return nilai
    return _TANPA_KATEGORI


@tool
async def get_menu() -> str:
    """Ambil daftar LENGKAP menu/produk aktif Toti Cakery beserta harganya.

    Gunakan saat pelanggan menanyakan menu, daftar kue, atau harga secara umum.
    Seluruh menu selalu dikirim, dikelompokkan per kategori.
    """
    # Tidak ada penyaringan sama sekali, dan tool ini sengaja tidak punya
    # parameter: model rutin menempelkan kategori yang tidak pernah diketik
    # pelanggan. Selama katalog belum punya kategori bernama "cake" itu tidak
    # terasa — filternya tidak cocok dengan apa pun dan seluruh menu tetap
    # keluar. Begitu katalog aslinya masuk, "menu dong" polos mulai menjawab 14
    # dari 23 produk. Pelanggan yang bertanya menu ingin melihat semuanya;
    # pengelompokan per kategori yang membuatnya tetap enak dibaca.
    # Konteks giliran boleh tidak ada: get_menu juga dipanggil di luar percakapan
    # (uji, pemanasan). Tanpa konteks, pakai bahasa bawaan daripada meledak.
    ctx = get_turn_context_or_none()
    lang = await store.get_lang(ctx.wa_number) if ctx else bahasa.DEFAULT
    items = await products_api.list_products(only_active=True)
    if not items:
        return bahasa.teks("menu_gagal", lang)

    grup: dict[str, list[dict]] = {}
    for p in items:
        grup.setdefault(_kategori(p), []).append(p)

    # Kategori diurutkan menurut abjad, tapi "Lainnya" selalu terakhir.
    urutan = sorted(grup, key=lambda k: (k == _TANPA_KATEGORI, k.casefold()))

    # Awalan "Berikut menu" dipertahankan: agent._history_view() memakainya untuk
    # mengganti seluruh blok ini dengan penanda, dan menu yang masuk kembali ke
    # konteks apa adanya itulah yang dulu mengajari model menjawab pertanyaan
    # menu dari ingatan.
    lines = [bahasa.teks("menu_judul", lang, toko=settings.store_name)]
    for nama_kategori in urutan:
        # Kapitalisasi disamakan saat ditampilkan: katalog memuat "Brownies",
        # "cake", "cookies", dan "Chiffon" berdampingan, dan pelanggan tidak
        # perlu ikut melihat ketidakrapian data itu.
        lines.append(f"\n*{nama_kategori.title()}*")
        for p in sorted(grup[nama_kategori], key=lambda x: product_label(x).casefold()):
            status = "" if tersedia(p) else bahasa.teks("menu_habis", lang)
            lines.append(f"• {product_label(p)} — {rupiah(p.get('harga_jual'))}{status}")
    lines.append(bahasa.teks("menu_penutup", lang))
    return "\n".join(lines)
