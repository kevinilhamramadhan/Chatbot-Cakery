"""Data-access helpers for the local SQLite store (sessions, logs, pending orders).

Each function manages its own AsyncSession so tools and background tasks can call
them freely without juggling a shared session.
"""

import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.conversation.states import State
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.conversation import ConversationLog
from app.models.pending_order import PendingOrder
from app.models.session import ChatSession

ACTIVE_ORDER_STATUSES = ("pending", "paid", "ready")


# ── Sessions ──────────────────────────────────────────────────────────────────
async def get_or_create_session(wa_number: str) -> ChatSession:
    async with async_session_factory() as db:
        row = await db.scalar(select(ChatSession).where(ChatSession.wa_number == wa_number))
        if row is not None:
            return row

    # WhatsApp customers routinely fire two or three messages in a row and the
    # webhook handles each in its own task, so the SELECT above can miss for all
    # of them at once. The loser of the INSERT race has to read the winner's row
    # instead of raising: live, two of three opening messages got no reply at
    # all (UNIQUE constraint failed: sessions.wa_number, swallowed in the log).
    async with async_session_factory() as db:
        row = ChatSession(wa_number=wa_number, state=State.IDLE)
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return await db.scalar(
                select(ChatSession).where(ChatSession.wa_number == wa_number)
            )
        await db.refresh(row)
        return row


async def update_session(wa_number: str, **fields) -> None:
    # Same race as above, one layer up: create through the guarded helper.
    await get_or_create_session(wa_number)
    async with async_session_factory() as db:
        row = await db.scalar(select(ChatSession).where(ChatSession.wa_number == wa_number))
        if row is None:  # deleted underneath us; nothing to update
            return
        for k, v in fields.items():
            setattr(row, k, v)
        await db.commit()


async def get_cart(wa_number: str) -> list[dict]:
    row = await get_or_create_session(wa_number)
    return json.loads(row.cart_json or "[]")


async def set_cart(wa_number: str, cart: list[dict]) -> None:
    await update_session(wa_number, cart_json=json.dumps(cart, ensure_ascii=False))


async def get_customer(wa_number: str) -> dict:
    row = await get_or_create_session(wa_number)
    return json.loads(row.customer_json or "{}")


async def set_customer(wa_number: str, customer: dict) -> None:
    await update_session(wa_number, customer_json=json.dumps(customer, ensure_ascii=False))


async def set_state(wa_number: str, state: State | str) -> None:
    await update_session(wa_number, state=str(state))


# ── Human takeover ────────────────────────────────────────────────────────────
async def activate_takeover(wa_number: str) -> datetime:
    expires = datetime.now(timezone.utc) + timedelta(days=settings.takeover_expiry_days)
    await update_session(
        wa_number, human_takeover_active=True, takeover_expires_at=expires
    )
    return expires


async def deactivate_takeover(wa_number: str) -> None:
    await update_session(
        wa_number, human_takeover_active=False, takeover_expires_at=None
    )


async def is_takeover_active(wa_number: str) -> bool:
    row = await get_or_create_session(wa_number)
    if not row.human_takeover_active:
        return False
    if row.takeover_expires_at is None:
        return True
    exp = row.takeover_expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) >= exp:
        await deactivate_takeover(wa_number)
        return False
    return True


# ── Conversation log ──────────────────────────────────────────────────────────
async def log_message(
    wa_number: str, direction: str, content: str, intent: str | None = None
) -> None:
    async with async_session_factory() as db:
        db.add(
            ConversationLog(
                wa_number=wa_number, direction=direction, content=content, intent=intent
            )
        )
        await db.commit()


async def recent_history(
    wa_number: str, limit: int = 6, exclude_last_user: str | None = None
) -> list[dict]:
    """Return recent turns as chat messages (oldest first) for LLM context.

    `exclude_last_user` drops a trailing user row with exactly that text. The
    orchestrator logs an inbound message BEFORE it routes, so without this the
    customer's current message comes back as the last history entry and the
    model sees it twice — two user turns in a row, a shape the fine-tuning data
    never contains. Measured on toti-qwen-1.7b-v5, that duplication alone flips
    "mau order cupcake dong" from add_to_cart 10/10 to escalate_to_admin 10/10.
    """
    async with async_session_factory() as db:
        rows = (
            await db.scalars(
                select(ConversationLog)
                .where(ConversationLog.wa_number == wa_number)
                .order_by(ConversationLog.id.desc())
                .limit(limit)
            )
        ).all()
    rows = list(reversed(rows))
    msgs = [
        {"role": "user" if r.direction == "in" else "assistant", "content": r.content}
        for r in rows
    ]
    if exclude_last_user and msgs and msgs[-1]["role"] == "user" \
            and msgs[-1]["content"] == exclude_last_user:
        msgs.pop()
    return msgs


# ── Pending orders ────────────────────────────────────────────────────────────
async def get_active_pending(wa_number: str) -> PendingOrder | None:
    async with async_session_factory() as db:
        return await db.scalar(
            select(PendingOrder)
            .where(
                PendingOrder.wa_number == wa_number,
                PendingOrder.status.in_(ACTIVE_ORDER_STATUSES),
            )
            .order_by(PendingOrder.id.desc())
        )


async def create_pending_order(**fields) -> PendingOrder:
    async with async_session_factory() as db:
        row = PendingOrder(**fields)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def update_pending_order(order_id: int, **fields) -> None:
    async with async_session_factory() as db:
        row = await db.get(PendingOrder, order_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        await db.commit()


async def purge_old_data() -> tuple[int, int]:
    """Delete stale personal data: old transcripts + identity blobs on finished
    orders (nama/alamat/nomor HP). The backend keeps the authoritative order
    record; this local DB is a working file and shouldn't age into an address
    book. Returns (logs_deleted, orders_scrubbed).
    """
    days = settings.data_retention_days
    if days <= 0:
        return 0, 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with async_session_factory() as db:
        res = await db.execute(
            delete(ConversationLog).where(ConversationLog.created_at < cutoff)
        )
        scrub = await db.execute(
            update(PendingOrder)
            .where(
                PendingOrder.created_at < cutoff,
                PendingOrder.status.notin_(ACTIVE_ORDER_STATUSES),
                PendingOrder.customer_json != "{}",
            )
            .values(customer_json="{}")
        )
        await db.commit()
        return res.rowcount or 0, scrub.rowcount or 0


async def list_orders_by_status(*statuses: str) -> list[PendingOrder]:
    async with async_session_factory() as db:
        return list(
            await db.scalars(
                select(PendingOrder).where(PendingOrder.status.in_(statuses))
            )
        )
