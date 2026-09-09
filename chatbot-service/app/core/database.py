"""Async SQLAlchemy setup for the chatbot's own local SQLite store.

This is internal storage owned by the chatbot service (sessions, conversation
log, pending orders) — NOT an addition to the backend (PROMPT §14).
"""

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, future=True)
async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


# Columns added after the first deploy. create_all() only creates missing
# TABLES, so a server that already has a chatbot_data volume would keep the old
# shape and every query touching a new column would fail with "no such column".
# SQLite has no "ADD COLUMN IF NOT EXISTS", so the existing columns are read first.
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "sessions": {"pending_escalation": "TEXT"},
    "pending_orders": {"nomor_invoice": "VARCHAR(64)", "pay_instruction": "TEXT"},
}


def _apply_added_columns(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
        existing = {row[1] for row in rows}
        if not existing:  # table wasn't there; create_all just made it correctly
            continue
        for name, ddl_type in columns.items():
            if name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}")


async def init_db() -> None:
    # Import models so their tables register on Base.metadata before create_all.
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_added_columns)

