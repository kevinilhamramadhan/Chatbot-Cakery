"""Small security helpers shared by the webhook, tools, and clients.

Kept dependency-free on purpose so it can be imported from anywhere (including
config-time code) without pulling in FastAPI or httpx.
"""

import re
import secrets

# A wweb.js chatId for an individual chat is `<digits>@c.us`; we also accept a
# bare number so internal callers (admin numbers from env/backend) work.
_WA_RE = re.compile(r"^\d{6,20}(@c\.us)?$")

_URL_RE = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)


def valid_wa_number(wa_number: str) -> bool:
    """True for a plausible individual-chat WhatsApp id.

    The whole authorization model of this service (order lookup, cancel, owner
    reports) hangs off this string, and it is also interpolated into backend URL
    paths — so anything that is not a plain number is rejected at the edge.
    """
    return bool(_WA_RE.match(wa_number or ""))


def wa_digits(wa_number: str) -> str:
    return "".join(c for c in (wa_number or "") if c.isdigit())


def mask_phone(wa_number: str) -> str:
    """`628123456789@c.us` -> `62***6789`. For logs: identifiable enough to
    correlate a session, not enough to harvest a customer list.
    """
    d = wa_digits(wa_number)
    if len(d) <= 6:
        return "***"
    return f"{d[:2]}***{d[-4:]}"


def token_matches(expected: str, provided: str | None) -> bool:
    """Constant-time compare that fails closed when nothing is configured."""
    if not expected:
        return False
    return secrets.compare_digest(expected, provided or "")


def sanitize_relay(text: str, limit: int = 200) -> str:
    """Neutralize free text before relaying it to a human on WhatsApp.

    The admin notification is delivered by the store's own number, so text a
    customer can steer (via the LLM's `reason` argument) must not be able to
    look like a system instruction or carry a clickable link.
    """
    cleaned = _URL_RE.sub("[link dihapus]", (text or "").strip())
    cleaned = cleaned.replace("\n", " ").replace("\r", " ")
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip() + " …"
    return cleaned or "(tidak ada keterangan)"
