"""Shared test setup. Must configure env BEFORE app modules import settings.

Every value here is FORCED, not defaulted. `os.environ.setdefault` looked
harmless but meant the suite silently inherited whatever the machine had:
inside the container (env_file: .env) `DATABASE_URL` points at the live
`toti_chatbot.db`, so `fresh_db` below dropped the real sessions/orders tables;
and `ADMIN_WA_NUMBER`/`OWNER_WA_NUMBERS` came in as the .env placeholders, which
made two takeover/report tests fail on that machine and pass everywhere else.
A test run must depend on nothing but this file.
"""

import os
import tempfile

_TMP_DB = os.path.join(tempfile.gettempdir(), "toti_test_chatbot.db")

os.environ.update({
    "DATABASE_URL": f"sqlite+aiosqlite:///{_TMP_DB}",
    "WEBHOOK_TOKEN": "test-webhook-token",
    "INTERNAL_API_KEY": "test-internal-key",
    "WWEBJS_API_KEY": "test-wwebjs-key",
    "ADMIN_WA_NUMBER": "628999000111",
    "OWNER_WA_NUMBERS": "628777000222",
    "AUTOFILL_PHONE_FROM_WA": "true",
    "ALLOW_DOWN_PAYMENT": "true",
    "DOWN_PAYMENT_PERCENTAGE": "0.5",
    "PAYMENT_TIMEOUT_MINUTES": "30",
    # Backend/Ollama must never be reachable from a test: every caller is
    # mocked, and an unmocked one should fail loudly rather than hit a real host.
    "BACKEND_BASE_URL": "http://backend.invalid",
    "OLLAMA_BASE_URL": "http://ollama.invalid",
})

import pytest_asyncio  # noqa: E402

from app.core.database import Base, engine  # noqa: E402

# Last line of defence: `fresh_db` drops every table, so a misconfigured run
# must not be able to point that at anything but the throwaway file above.
assert str(engine.url).endswith("toti_test_chatbot.db"), (
    f"refusing to run tests against {engine.url!r} — drop_all would destroy it"
)


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    """Recreate all tables before each test for isolation."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
