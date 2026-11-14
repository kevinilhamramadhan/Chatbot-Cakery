"""Shared helpers for tools: currency formatting + product name resolution."""

import re

from app.backend_client import products as products_api


def rupiah(value) -> str:
    try:
        return f"Rp{int(round(float(value))):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "harga belum tersedia"


def product_label(p: dict) -> str:
    return p.get("nama_produk") or p.get("nama") or f"Produk #{p.get('id')}"


def tersedia(p: dict) -> bool:
    """Apakah produk ini benar-benar bisa dipesan sekarang.

    Backend menghitung `is_in_stock = is_available and stock_quantity > 0`, dan
    POST /orders menolak 400 "Stok produk ... sedang habis" untuk produk yang
    stoknya nol. `is_available` saja tidak cukup: backend membiarkannya True
    sementara stoknya nol, jadi pelanggan baru tahu kuenya habis setelah seluruh
    data checkout diisi. Kalau backend belum mengirim field-nya, dianggap ada.
    """
    if "is_in_stock" in p:
        return bool(p["is_in_stock"])
    return bool(p.get("is_available", True))


# Indonesian/English spelling variants that must fold to one token, otherwise
# "brownies cokelat" scores zero against "Brownies Coklat" on the flavour word
# and the match falls back to whatever else shares a token (observed live: the
# LLM writes "Cokelat", the catalogue says "Coklat"). Keys are post-plural-strip.
_SPELLING = {
    "cokelat": "coklat", "chocolate": "coklat", "choco": "coklat", "cklt": "coklat",
    "vanila": "vanilla", "vanili": "vanilla", "vanile": "vanilla",
    "stroberi": "strawberry", "strawbery": "strawberry", "strobery": "strawberry",
    "kuki": "cookie", "cooky": "cookie",
    "brownie": "browni", "broni": "browni", "bronis": "browni",
    "keju": "cheese", "matca": "matcha",
    "tar": "tart",
}

# "10 cm" -> "10cm": the catalogue writes sizes closed up, customers don't.
_SIZE_RE = re.compile(r"(\d+)\s*(cm|inch|in)\b")
# "ukuran 20" / "diameter 20" -> "20cm": ukuran yang ditulis tanpa satuan. Hanya
# sesudah kata ukuran, karena angka polos ("cake vanilla 2") adalah jumlah.
_UKURAN_RE = re.compile(r"\b(?:ukuran|diameter|size|uk)\.?\s*(\d+)\b(?!\s*(?:cm|inch|in)\b)")


def _kata(s: str) -> list[str]:
    """Kata yang sudah dinormalkan, berurutan dan boleh berulang (lihat _tokens)."""
    # strip punctuation, then rstrip("s") folds singular/plural ("cupcake" ==
    # "cupcakes") — without it a bare "cupcake" matched one variant arbitrarily.
    # Finally fold known spelling variants so "cokelat" == "coklat".
    s = _SIZE_RE.sub(r"\1\2", _UKURAN_RE.sub(r"\1cm", s.lower()))
    out = []
    for raw in s.split():
        w = raw.strip(".,?!()\"'").rstrip("s")
        if not w:
            continue
        out.append(_SPELLING.get(w, w))
    return out


def _tokens(s: str) -> set[str]:
    return set(_kata(s))


def disebut_lengkap(teks: str, kandidat: list[dict]) -> dict | None:
    """Kandidat yang SELURUH kata namanya ditulis pelanggan, kalau hanya satu.

    Model 1,7 B kadang memotong angka dari nama kue dan memakainya sebagai
    jumlah: "saya mau Cupcakes Cokelat Isi 4" menjadi product="Cupcakes Cokelat
    Isi", qty=4, lalu bot bertanya "Isi 4, Isi 6, atau Isi 9?" padahal pelanggan
    sudah menulis nama lengkapnya (produksi, 9 Nov 2026). Ini mencocokkan teks
    pelanggan dengan nama di katalog, bukan menebak maksud.
    """
    kata = set(_kata(teks or ""))
    cocok = [p for p in kandidat if (t := _tokens(product_label(p))) and t <= kata]
    if not cocok:
        return None
    terpanjang = max(len(_tokens(product_label(p))) for p in cocok)
    cocok = [p for p in cocok if len(_tokens(product_label(p))) == terpanjang]
    return cocok[0] if len(cocok) == 1 else None


def sisa_tanpa_nama(teks: str, p: dict) -> str | None:
    """Teks pelanggan tanpa kata-kata nama kue `p`; None kalau namanya tidak ditulis lengkap."""
    kata = _kata(teks or "")
    for t in _kata(product_label(p)):
        if t not in kata:
            return None
        kata.remove(t)
    return " ".join(kata)


async def resolve_product(query: str) -> tuple[dict | None, list[dict]]:
    """Find a product by id or (fuzzy) name from the live backend list.

    Scores every candidate by token overlap; the BEST match wins. Returns
    (match, candidates):
      (product, [])     -> confident single best match
      (None, [a, b, …]) -> AMBIGUOUS: several products tie for best ("cupcake"
                           fits isi 4/6/9…). Callers must ASK, never guess —
                           guessing is how "4 cupcake" became "isi 6 x4".
      (None, [])        -> not found
    """
    query = query.strip()
    if query.isdigit():
        p = await products_api.get_product(int(query))
        if p:
            return p, []
    items = await products_api.list_products(only_active=True)
    q = query.lower()
    q_tokens = _tokens(q)
    if not q_tokens:
        return None, []

    scored: list[tuple[float, dict]] = []
    for p in items:
        label = product_label(p).lower()
        if label == q:
            return p, []  # exact name always wins
        l_tokens = _tokens(label)
        overlap = q_tokens & l_tokens
        if not overlap:
            continue
        # Matched-token count dominates (specific beats generic); coverage of
        # label+query breaks ties toward the tighter name.
        score = len(overlap) * 2 + len(overlap) / len(l_tokens) + len(overlap) / len(q_tokens)
        # "brownies" cocok ke Brownies Coklat (habis) dan Brownies Fudgy Almond
        # (ada): yang bisa dipesan didahulukan. Bonusnya di bawah nilai satu
        # kata cocok, jadi nama yang disebut lengkap tetap menang.
        if tersedia(p):
            score += 0.5
        scored.append((score, p))

    if not scored:
        return None, []
    best_score = max(s for s, _ in scored)
    best = [p for s, p in scored if s >= best_score - 1e-9]
    if len(best) == 1:
        return best[0], []
    return None, best


def options_line(candidates: list[dict], limit: int = 6) -> str:
    """'Cupcakes isi 4 (Rp40.000), Cupcakes isi 6 (Rp55.000), …' for ask-backs."""
    return ", ".join(
        f"{product_label(p)} ({rupiah(p.get('harga_jual'))})" for p in candidates[:limit]
    )


async def menu_fallback(lead: str, lang: str | None = None) -> str:
    """`lead` followed by the catalogue itself, for a product we couldn't resolve.

    "Coba cek menu dulu ya" asked the customer to do work the bot could just do.
    A reference the model cannot resolve — "yang coklat itu lho" — is exactly the
    moment to show what there is, instead of sending them away. Falls back to the
    old wording when the catalogue can't be read.
    """
    try:
        items = await products_api.list_products(only_active=True)
    except Exception:  # noqa: BLE001 - the caller still has something to say
        items = []
    from app.conversation import bahasa

    # Hanya yang bisa dipesan sekarang: daftar ini menjawab "adanya apa?", dan
    # kue habis yang tampil tanpa tanda (uji UAT 31 Okt 2026) mengundang pesanan
    # yang ditolak di langkah berikutnya.
    items = [p for p in items if tersedia(p)]
    if not items:
        return bahasa.teks("menu_fallback_kosong", lang, lead=lead)
    lines = "\n".join(
        f"• {product_label(p)} — {rupiah(p.get('harga_jual'))}" for p in items
    )
    return bahasa.teks("menu_fallback", lang, lead=lead, daftar=lines)
