"""Per-customer conversation session (keyed by WhatsApp number)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ChatSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Current conversation state (see app/conversation/states.py).
    state: Mapped[str] = mapped_column(String(40), default="idle")

    # Draft cart + collected identity, stored as JSON strings.
    cart_json: Mapped[str] = mapped_column(Text, default="[]")
    customer_json: Mapped[str] = mapped_column(Text, default="{}")

    # An escalation the bot has OFFERED but the customer has not accepted yet.
    # Holds the reason text; takeover only starts once they say yes.
    pending_escalation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Human takeover flag — while active the bot stops auto-replying.
    human_takeover_active: Mapped[bool] = mapped_column(Boolean, default=False)
    takeover_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
