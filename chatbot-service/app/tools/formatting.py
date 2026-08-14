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


def _tokens(s: str) -> set[str]:
    # strip punctuation, then rstrip("s") folds singular/plural ("cupcake" ==
    # "cupcakes") — without it a bare "cupcake" matched one variant arbitrarily.
    # Finally fold known spelling variants so "cokelat" == "coklat".
    s = _SIZE_RE.sub(r"\1\2", s.lower())
    out = set()
    for raw in s.split():
        w = raw.strip(".,?!()\"'").rstrip("s")
        if not w:
            continue
        out.add(_SPELLING.get(w, w))
    return out


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
