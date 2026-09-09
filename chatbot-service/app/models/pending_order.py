"""Pending order tracking for payment timeout + auto paid-detection (PROMPT §10)."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PendingOrder(Base):
    __tablename__ = "pending_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_number: Mapped[str] = mapped_column(String(32), index=True)

    # Backend order id (as string) and the Midtrans transaction ref.
    order_ref: Mapped[str] = mapped_column(String(64), index=True)
    payment_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    payment_type: Mapped[str] = mapped_column(String(8), default="full")  # full|dp
    total_amount: Mapped[float] = mapped_column(Float)   # full order value
    amount_due: Mapped[float] = mapped_column(Float)     # what must be paid now

    # pending | paid | expired | cancelled | ready
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)

    # What the customer was actually told at checkout. Kept so every later
    # message can name the same invoice, and so the payment link can be sent
    # again — on WhatsApp the checkout message is buried within minutes.
    nomor_invoice: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pay_instruction: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Snapshots so notifications work without re-querying.
    items_json: Mapped[str] = mapped_column(Text, default="[]")
    customer_json: Mapped[str] = mapped_column(Text, default="{}")
    delivery_method: Mapped[str] = mapped_column(String(12), default="pickup")

    # Guards so the background task never double-notifies.
    notified_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_ready: Mapped[bool] = mapped_column(Boolean, default=False)

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
