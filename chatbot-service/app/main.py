"""FastAPI entrypoint for the Toti Cakery chatbot service."""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.conversation import background
from app.core.config import settings
from app.core.database import init_db
from app.webhook.routes import router as webhook_router
from app.webhook.routes import wa_router

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


class _RedactAccessLog(logging.Filter):
    """Keep the webhook secret and phone numbers out of uvicorn's access log.

    The gateway cannot attach headers to its callbacks, so the shared secret
    travels as a path segment — and uvicorn wrote it verbatim to the access log,
    on disk, once per inbound message. That token is the only thing stopping
    someone from forging a message from any customer's number. The internal
    endpoints carry a phone number in the path for the same structural reason.
    """

    _PATTERNS = (
        re.compile(r"(/webhook/whatsapp/)[^\s\"?]+"),
        re.compile(r"(/webhook/internal/takeover/)[^\s\"/?]+"),
    )

    def _sensor(self, teks: str) -> str:
        for pattern in self._PATTERNS:
            teks = pattern.sub(r"\1***", teks)
        return teks

    def filter(self, record: logging.LogRecord) -> bool:
        # Yang disensor adalah ARGUMEN path-nya, bukan pesan jadinya. uvicorn
        # AccessFormatter membongkar record.args jadi 5 nilai
        # (client_addr, method, full_path, http_version, status_code); kalau
        # filter ini menaruh pesan jadi di record.msg lalu mengosongkan args --
        # seperti versi sebelumnya -- formatter itu jatuh dengan
        # "ValueError: not enough values to unpack (expected 5, got 0)" dan
        # SETIAP webhook mencetak "--- Logging error ---" + traceback ke log.
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            disensor = self._sensor(args[2])
            if disensor != args[2]:
                record.args = args[:2] + (disensor,) + args[3:]
            return True
        # Bentuk lain (pesan yang sudah jadi string): tidak ada placeholder yang
        # perlu diisi, jadi aman menimpa msg dan mengosongkan args.
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging over formatting
            return True
        redacted = self._sensor(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


logging.getLogger("uvicorn.access").addFilter(_RedactAccessLog())


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
    from langchain_core.messages import HumanMessage

    from app.conversation import bahasa
    from app.llm.agent import pesan_pembuka
    from app.llm.client import get_llm
    from app.rag.embeddings import get_embedding_function
    from app.tools.registry import ALL_TOOLS, TOOLS_UMUM

    # Sesudah VM menyala, chatbot biasanya hidup lebih dulu daripada Ollama
    # (yang masih memuat model dan menarik GGUF-nya). Dulu satu kegagalan di sini
    # berarti pemanasan dilewati sama sekali dan pelanggan pertama tiap bahasa
    # menanggung prefill penuh; sekarang dicoba ulang sampai Ollama siap.
    for percobaan in range(1, 31):
        try:
            await asyncio.to_thread(get_embedding_function().embed_one, "warmup")
            # Setiap bentuk prefix yang dipakai giliran sungguhan: arahan bahasa
            # dan daftar tool ikut di dalam prefix, jadi pelanggan Indonesia,
            # pelanggan Inggris, dan Owner masing-masing punya bentuk sendiri.
            # Ollama menyimpan beberapa prompt sekaligus, jadi keempatnya tetap
            # hangat. Pelanggan Indonesia duluan — itu yang paling sering datang.
            for tools in (TOOLS_UMUM, ALL_TOOLS):
                for lang in (bahasa.ID, bahasa.EN):
                    pembuka, pengingat = pesan_pembuka(lang)
                    await get_llm().bind_tools(tools).ainvoke(
                        [pembuka, pengingat, HumanMessage(content="halo")])
            logger.info("Model warm-up complete — models resident + prefix cache primed.")
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Model warm-up attempt %d failed (%s); retrying in 30s",
                           percobaan, exc)
            await asyncio.sleep(30)
    logger.warning("Model warm-up gave up; the first request will load the model.")


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


# Interactive docs are a development convenience. In production they describe
# the internal endpoints (and the shape of the webhook path) to anyone who can
# reach the service on the compose network.
_is_production = settings.environment.lower() == "production"

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _is_production else "/docs",
    redoc_url=None if _is_production else "/redoc",
    openapi_url=None if _is_production else "/openapi.json",
)
app.include_router(webhook_router, prefix="/webhook", tags=["webhook"])
# Alias di akar: backend memanggil {CHATBOT_URL}/status, /qr, /ganti-nomor.
# Kuncinya tetap X-Internal-Key, dan layanan ini tidak pernah terbuka ke publik.
app.include_router(wa_router, tags=["webhook"])


@app.get("/health")
async def health():
    return {"status": "ok", "service": settings.app_name}
