"""Conversation states for the order flow (PROMPT §10)."""

import re
from enum import StrEnum


class State(StrEnum):
    IDLE = "idle"                       # general Q&A / browsing; LLM + tools
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    COLLECTING_IDENTITY = "collecting_identity"
    AWAITING_PAYMENT = "awaiting_payment"
    # Pesanan yang SUDAH dibayar hanya dibatalkan setelah pelanggan menjawab ya:
    # konsekuensinya uang berpindah, jadi tidak boleh ditebak dari satu kalimat.
    AWAITING_CANCEL_CONFIRMATION = "awaiting_cancel_confirm"
    ORDER_ACTIVE = "order_active"       # paid, awaiting ready/pickup


# Simple affirmative/cancel keyword sets for deterministic steps.
CONFIRM_WORDS = {
    "sudah", "sudah sesuai", "sesuai", "betul", "benar", "bener", "ya", "yes",
    "ok", "oke", "okay", "lanjut", "lanjutkan", "fix", "gas", "iya", "setuju",
    "confirm", "boleh", "siap", "sip", "yup", "deal",
}
CANCEL_WORDS = {
    "batal", "batalkan", "cancel", "gajadi", "gak jadi", "ga jadi", "tidak jadi",
    "stop",
}

_PUNCT_RE = re.compile(r"[^\w\s]+", re.UNICODE)

_CONFIRM_PHRASES = (
    "sudah sesuai", "udah sesuai", "lanjut bayar", "sudah benar", "udah benar",
    "sudah bener", "udah bener", "sudah betul", "udah betul", "sudah pas",
    "udah pas", "sudah fix", "udah fix", "sudah cocok", "udah cocok",
)


def _tokens(text: str) -> list[str]:
    """Lowercase words, punctuation stripped: "iya, udah bener!" -> [iya, udah, bener]."""
    return _PUNCT_RE.sub(" ", (text or "").lower()).split()


def text_is_confirm(text: str) -> bool:
    """True when the customer is agreeing — not only when they typed the magic words.

    The old rule compared the whole message against CONFIRM_WORDS, which is why
    "iya udah bener" fell through to the LLM at the cart-confirmation step: the
    model then called add_to_cart again with the same item and the order doubled
    (2 brownies -> 4, measured live). A message that also carries a number is not
    treated as agreement by the caller — "iya, tambah 1 lagi" is a change.
    """
    words = _tokens(text)
    if not words:
        return False
    joined = " ".join(words)
    if joined in CONFIRM_WORDS or any(p in joined for p in _CONFIRM_PHRASES):
        return True
    # A short agreement with filler around it: "iya udah bener", "oke gas".
    return len(words) <= 4 and any(w in CONFIRM_WORDS for w in words)


_TERIMA_KASIH = (
    "makasih", "terima kasih", "terimakasih", "thanks", "thank you", "thx",
    "tengkyu", "tengkiu", "tq", "suwun", "nuhun",
)


def text_is_gratitude(text: str) -> bool:
    """Ucapan terima kasih — penutup percakapan, bukan persetujuan.

    Dipakai hanya di tempat yang taruhannya besar (tawaran sambung ke admin).
    Terukur hidup: "makasih ya kak" lolos sebagai konfirmasi karena "ya" ada di
    CONFIRM_WORDS dan kalimatnya pendek, lalu bot ikut diam sehari penuh karena
    takeover telanjur menyala. Di langkah konfirmasi keranjang aturan ini tidak
    dipakai: "oke makasih" di sana memang berarti setuju.
    """
    t = " ".join(_tokens(text))
    return any(w in t for w in _TERIMA_KASIH)


def text_is_cancel(text: str) -> bool:
    t = " ".join(_tokens(text))
    return any(w in t for w in CANCEL_WORDS)


# Angka yang benar-benar ditulis pelanggan — dipakai untuk menolak jumlah
# yang cuma ditebak model, bukan untuk menebak maksud.
NUMBER_WORDS = {
    "nol": 0, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
    "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "selusin": 12,
    "lusin": 12,
}




def mentions_quantity(text: str) -> bool:
    """Whether the customer actually wrote a number anywhere in the message.

    Not intent detection: it answers "did a human put this number here?", which
    is what stops the model from inventing a quantity ("beberapa" -> 2) and from
    growing a cart line on a message that contains no number at all.
    """
    words = _tokens(text)
    return any(w.isdigit() for w in words) or any(w in NUMBER_WORDS for w in words)
