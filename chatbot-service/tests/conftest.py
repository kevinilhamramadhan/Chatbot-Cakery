"""Shared test setup. Must configure env BEFORE app modules import settings."""

import os
import tempfile

# Point the local DB at a throwaway file and set deterministic config.
_TMP_DB = os.path.join(tempfile.gettempdir(), "toti_test_chatbot.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_TMP_DB}")
os.environ.setdefault("WEBHOOK_TOKEN", "test-webhook-token")
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")
os.environ.setdefault("WWEBJS_API_KEY", "test-wwebjs-key")
os.environ.setdefault("ADMIN_WA_NUMBER", "628999000111")
os.environ.setdefault("OWNER_WA_NUMBERS", "628777000222")
os.environ.setdefault("AUTOFILL_PHONE_FROM_WA", "true")
os.environ.setdefault("ALLOW_DOWN_PAYMENT", "true")
os.environ.setdefault("DOWN_PAYMENT_PERCENTAGE", "0.5")
os.environ.setdefault("PAYMENT_TIMEOUT_MINUTES", "30")

import pytest_asyncio  # noqa: E402

from app.core.database import Base, engine  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    """Recreate all tables before each test for isolation."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
