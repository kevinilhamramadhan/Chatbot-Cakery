"""Conversation states for the order flow (PROMPT §10)."""

import re
from enum import StrEnum


class State(StrEnum):
    IDLE = "idle"                       # general Q&A / browsing; LLM + tools
    AWAITING_CART_CONFIRMATION = "awaiting_cart_confirmation"
    COLLECTING_IDENTITY = "collecting_identity"
    AWAITING_PAYMENT = "awaiting_payment"
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
    (2 brownies -> 4, measured live). Callers must test `looks_like_cart_change()`
    FIRST so that "iya, tambah 1 lagi" counts as a change, not as agreement.
    """
    words = _tokens(text)
    if not words:
        return False
    joined = " ".join(words)
    if joined in CONFIRM_WORDS or any(p in joined for p in _CONFIRM_PHRASES):
        return True
    # A short agreement with filler around it: "iya udah bener", "oke gas".
    return len(words) <= 4 and any(w in CONFIRM_WORDS for w in words)


def text_is_cancel(text: str) -> bool:
    t = " ".join(_tokens(text))
    return any(w in t for w in CANCEL_WORDS)


# ── Cart-change intent ────────────────────────────────────────────────────────
NUMBER_WORDS = {
    "nol": 0, "satu": 1, "dua": 2, "tiga": 3, "empat": 4, "lima": 5, "enam": 6,
    "tujuh": 7, "delapan": 8, "sembilan": 9, "sepuluh": 10, "selusin": 12,
    "lusin": 12,
}
_CHANGE_VERBS = (
    "tambah", "nambah", "ganti", "ubah", "kurang", "hapus", "buang", "naikin",
    "naikkan", "turunin", "turunkan", "tukar",
)
_ORDER_WORDS = {"lagi", "aja", "dong", "pesan", "pesen", "mau", "minta", "beli", "order"}


def looks_like_cart_change(text: str) -> bool:
    """True when the message asks to change the order instead of answering a step.

    Checked before both confirmation and identity collection. Without it,
    "eh tambahin 1 brownies fudgy almond dong" typed at the name step was saved
    as the customer's name and sent to the backend that way (observed live).
    """
    words = _tokens(text)
    if not words:
        return False
    if any(v in w for w in words for v in _CHANGE_VERBS):
        return True
    has_number = any(w.isdigit() for w in words) or any(w in NUMBER_WORDS for w in words)
    return has_number and any(w in _ORDER_WORDS for w in words)


def mentions_quantity(text: str) -> bool:
    """Whether the customer actually wrote a number anywhere in the message."""
    words = _tokens(text)
    return any(w.isdigit() for w in words) or any(w in NUMBER_WORDS for w in words)


_GREETING_WORDS = {
    "halo", "hallo", "hai", "hi", "hey", "kak", "kakak", "min", "admin", "p",
    "pagi", "siang", "sore", "malam", "assalamualaikum", "permisi", "misi",
    "selamat", "hello",
}


_ADMIN_REQUESTS = (
    "sambungkan ke admin", "sambungkan admin", "sambungin ke admin",
    "sambungin admin", "hubungkan ke admin", "hubungkan admin", "hubungi admin",
    "bicara dengan admin", "bicara sama admin", "ngobrol sama admin",
    "chat dengan admin", "chat sama admin", "panggilkan admin", "panggil admin",
    "mau ke admin", "ke admin aja", "sama admin aja", "dengan admin aja",
)


def asks_for_admin(text: str) -> bool:
    """The customer asking, in so many words, to be handed to a human.

    Deterministic on purpose. The offer the bot makes is stored for one turn
    only, and the model does not reliably re-issue it: live, "eh iya deh,
    sambungkan ke admin aja" was answered with the offer text WITHOUT calling
    the tool, so the customer's next "ya" had nothing to accept and they were
    asked the same question again. An explicit request needs no confirmation.
    """
    t = " ".join(_tokens(text))
    return any(p in t for p in _ADMIN_REQUESTS)


def is_bare_greeting(text: str) -> bool:
    """A greeting and nothing else — the commonest opening message on WhatsApp.

    Answered from a template instead of the model: "kak" used to come back as
    "Maaf, aku belum bisa jawab soal itu", and it also cost ~8s of inference on
    the very first message of every conversation.
    """
    words = _tokens(text)
    if not words or len(words) > 3:
        return False
    return all(w in _GREETING_WORDS for w in words)
