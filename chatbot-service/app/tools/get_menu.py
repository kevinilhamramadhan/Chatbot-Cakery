"""Tool: get_menu — REAL, hits the backend product list."""

from langchain_core.tools import tool

from app.backend_client import products as products_api
from app.tools.formatting import product_label, rupiah

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
    items = await products_api.list_products(only_active=True)
    if not items:
        return "Maaf, daftar menu sedang tidak bisa diambil. Coba lagi sebentar lagi ya."

    grup: dict[str, list[dict]] = {}
    for p in items:
        grup.setdefault(_kategori(p), []).append(p)

    # Kategori diurutkan menurut abjad, tapi "Lainnya" selalu terakhir.
    urutan = sorted(grup, key=lambda k: (k == _TANPA_KATEGORI, k.casefold()))

    # Awalan "Berikut menu" dipertahankan: agent._history_view() memakainya untuk
    # mengganti seluruh blok ini dengan penanda, dan menu yang masuk kembali ke
    # konteks apa adanya itulah yang dulu mengajari model menjawab pertanyaan
    # menu dari ingatan.
    lines = ["Berikut menu Toti Cakery:"]
    for nama_kategori in urutan:
        lines.append(f"\n*{nama_kategori}*")
        for p in sorted(grup[nama_kategori], key=lambda x: product_label(x).casefold()):
            # is_available dihitung backend (resep vs stok). Absen -> tersedia.
            status = "" if p.get("is_available", True) else "  (sedang tidak tersedia)"
            lines.append(f"• {product_label(p)} — {rupiah(p.get('harga_jual'))}{status}")
    lines.append("\nMau lihat detail salah satu kue? Sebutkan namanya ya 😊")
    return "\n".join(lines)
