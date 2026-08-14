"""FastAPI entrypoint for the Toti Cakery chatbot service."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.conversation import background
from app.core.config import settings
from app.core.database import init_db
from app.webhook.routes import router as webhook_router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def _warmup_models() -> None:
    """Preload the models AND prime Ollama's prefix cache so the FIRST real user
    doesn't pay a cold start.

    Two separate costs are being paid here. Loading the weights (~1 min on CPU)
    is the obvious one. The other is prompt prefill: the system prompt plus the
    9 tool definitions is ~2.5k tokens, and prefilling it on CPU takes ~60s. So
    the warm-up sends the exact same constant prefix a real turn sends — same
    system block, same bound tools — which leaves it in Ollama's KV cache. Every
    later turn then reuses it and only prefills the short tail (history +
    question). Sending a bare "warmup" string would load the weights but leave
    the first customer waiting a minute.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm.client import get_llm
    from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER
    from app.rag.embeddings import get_embedding_function
    from app.tools.registry import ALL_TOOLS

    try:
        await asyncio.to_thread(get_embedding_function().embed_one, "warmup")
        await get_llm().bind_tools(ALL_TOOLS).ainvoke([
            SystemMessage(content=SYSTEM_PROMPT),
            SystemMessage(content=TOOL_REMINDER),
            HumanMessage(content="halo"),
        ])
        logger.info("Model warm-up complete — models resident + prefix cache primed.")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Model warm-up skipped (will load on first request): %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Refuse to serve with unset/placeholder secrets: the webhook token is the
    # only thing standing between this service and anyone forging a message
    # from any customer's number.
    settings.validate_runtime()
    await init_db()
    background.start()
    if settings.warmup_on_startup:
        # Fire-and-forget: don't block startup on the ~1 min cold load.
        asyncio.create_task(_warmup_models())
        logger.info("%s started (warming models in background).", settings.app_name)
    else:
        logger.info("%s started (model warm-up disabled).", settings.app_name)
    yield
    await background.stop()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.app_name}
