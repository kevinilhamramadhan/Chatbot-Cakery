"""Request-scoped context shared with LangChain tools.

Tools receive only their declared arguments, but they also need to know which
customer is talking and a place to queue side-effects (e.g. "send this product
image"). We pass that through context variables set for the duration of one
incoming message.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class OutboundMedia:
    image_url: str
    caption: str | None = None


@dataclass
class TurnContext:
    wa_number: str
    # The customer's raw message. Tools need it to check the model's arguments
    # against what was actually typed — add_to_cart turned "pesan bolu beberapa"
    # into qty=2, a number nobody wrote.
    user_text: str = ""
    media: list[OutboundMedia] = field(default_factory=list)
    # A tool may request a hard state transition handled by the orchestrator.
    next_state: str | None = None
    # Filled during the turn so a single log line can describe what happened.
    tools_called: list[str] = field(default_factory=list)
    rag_similarity: float | None = None
    rag_in_scope: bool | None = None


_current: ContextVar[TurnContext | None] = ContextVar("turn_context", default=None)


def set_turn_context(ctx: TurnContext) -> None:
    _current.set(ctx)


def get_turn_context_or_none() -> TurnContext | None:
    """Like get_turn_context(), but for callers that also run outside a turn
    (background jobs, tests) and only want to record something if one exists."""
    return _current.get()


def get_turn_context() -> TurnContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("No TurnContext set for this turn")
    return ctx
