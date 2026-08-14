"""Dependency-light unit tests (no Ollama / backend / DB needed)."""

from app.conversation.states import text_is_cancel, text_is_confirm
from app.rag.store import RetrievalResult
from app.tools.add_to_cart import cart_summary
from app.tools.formatting import rupiah


def test_rupiah_formats_thousands():
    assert rupiah(150000) == "Rp150.000"
    assert rupiah(1500) == "Rp1.500"
    assert rupiah(None) == "harga belum tersedia"


def test_confirm_and_cancel_keywords():
    assert text_is_confirm("sudah sesuai")
    assert text_is_confirm("ya")
    assert text_is_cancel("batalkan")
    assert text_is_cancel("ga jadi")
    assert not text_is_confirm("mau nambah lagi")
    assert not text_is_cancel("lanjut")


def test_cart_summary_totals():
    cart = [
        {"nama": "Brownies", "harga": 50000, "qty": 2},
        {"nama": "Bolu", "harga": 75000, "qty": 1},
    ]
    out = cart_summary(cart)
    assert "Brownies x2" in out
    assert "Rp175.000" in out  # 2*50000 + 75000


def test_spelling_variants_fold_to_one_token():
    """Live regression: the LLM writes 'Brownies 10cm Cokelat', the catalogue says
    'Brownies Coklat'. Without folding, the flavour word scores zero and the
    spurious '10cm' token made 'Cake 10cm' an equally good match (ambiguous)."""
    from app.tools.formatting import _tokens

    assert _tokens("Brownies Cokelat") == _tokens("brownies coklat")
    assert _tokens("Cake 10 cm") == _tokens("cake 10cm")
    assert _tokens("bento kukis 10cm") == _tokens("Bento Cookies 10cm")
    # A real distinction must survive the folding.
    assert _tokens("Cupcakes isi 4") != _tokens("Cupcakes isi 6")


def test_scope_guard_threshold():
    in_scope = RetrievalResult(["doc"], [{}], [0.8])
    out_scope = RetrievalResult(["doc"], [{}], [0.1])
    assert in_scope.in_scope is True
    assert out_scope.in_scope is False
    assert RetrievalResult([], [], []).best_similarity == 0.0


def test_context_drops_chunks_below_threshold():
    """retrieve() always returns top_k; only the chunks that clear the threshold
    on their own score belong in the prompt. Live case: asking about delivery
    pulled pengiriman (0.404) plus pembayaran (0.348) and halal (0.299)."""
    r = RetrievalResult(
        ["pengiriman", "pembayaran", "halal"], [{}, {}, {}], [0.404, 0.348, 0.299]
    )
    assert r.in_scope is True
    assert r.relevant_documents == ["pengiriman"]
    assert r.context_text() == "pengiriman"
    # Several genuinely-relevant chunks still all go through.
    r2 = RetrievalResult(["a", "b"], [{}, {}], [0.7, 0.55])
    assert r2.relevant_documents == ["a", "b"]
