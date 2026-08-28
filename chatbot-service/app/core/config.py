"""Central configuration for the Toti Cakery chatbot service.

Every tunable lives here and is sourced from environment variables so nothing
operational is hardcoded in business logic (see PROMPT rules #2 and #4).
"""

import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Service ───────────────────────────────────────────────────────────────
    app_name: str = "Toti Cakery Chatbot Service"
    log_level: str = "INFO"
    # Log message BODIES (customer text) as well as metadata. Off by default:
    # the conversation log in SQLite is the record of truth, and container logs
    # are read by more people/tools than the DB is.
    log_message_bodies: bool = False

    # ── Webhook / internal endpoint auth ──────────────────────────────────────
    # wwebjs-api cannot attach an auth header to its callbacks, but the callback
    # URL is ours to choose — so the shared secret rides in the path:
    #   BASE_WEBHOOK_URL=http://chatbot-service:8000/webhook/whatsapp/<token>
    # Without it, anyone able to reach this service can forge a message from any
    # WhatsApp number and act as that customer. Required at startup.
    webhook_token: str = ""
    # Guards /webhook/internal/* (ends a human takeover, pushes "order ready"
    # messages to real customers). Sent as X-Internal-Key. Required at startup.
    internal_api_key: str = ""

    # ── Backend (teammate's FastAPI) ──────────────────────────────────────────
    backend_base_url: str = "https://backend-cakery.vercel.app"
    # 30s, not 10: the backend runs on Vercel serverless and a cold start
    # regularly exceeds 10s — the chatbot then tells the customer "menu sedang
    # tidak bisa diambil" for what is really just a warm-up.
    backend_request_timeout_seconds: float = 30.0
    # Sent as X-Service-Key on every backend call. Must match the backend's
    # SERVICE_API_KEY (require_service_key dependency). Harmless on public GETs.
    backend_service_api_key: str = ""

    # ── Ollama (LLM + embeddings) ─────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    # v4 fine-tune. v3 (toti-qwen-1.7b) mangles product names against the current
    # catalogue — measured live: it invents "Brownies 10cm Cokelat" for "brownies
    # coklat" and answers menu questions without calling get_menu. Don't go back.
    llm_model: str = "toti-qwen-1.7b-v4"
    embedding_model: str = "qwen3-embedding:0.6b"
    llm_temperature: float = 0.7
    llm_top_p: float = 0.8
    llm_num_ctx: int = 32768
    # Hard cap on generated tokens per reply, and the main lever on the latency
    # TAIL. CPU generation runs ~10 tok/s here, so 768 tokens = ~77s — measured:
    # a rambling turn hit 120s. 384 keeps the worst case near 40s and is still
    # far more than a WhatsApp reply needs (tool calls are ~30 tokens; the long
    # outputs customers see, like the menu, are printed by the TOOL, not
    # generated). Raise only if replies start getting cut off mid-sentence.
    llm_num_predict: int = 384
    # Keep the LLM + embedding models resident in Ollama's RAM instead of
    # unloading after idle, in SECONDS: -1 = forever, or a positive count to
    # auto-unload (e.g. 300 = 5m). Must be an int: OllamaEmbeddings rejects a
    # duration string, and ChatOllama rejects a bare "-1".
    ollama_keep_alive: int = -1
    # Preload the models into RAM on service startup (main.lifespan) so the first
    # real user never pays the ~1min cold load. Turn OFF on a dev laptop to keep
    # RAM free until you actually chat: WARMUP_ON_STARTUP=false (+ a positive
    # OLLAMA_KEEP_ALIVE so idle models unload).
    warmup_on_startup: bool = True

    # ── RAG / ChromaDB ────────────────────────────────────────────────────────
    chroma_persist_dir: str = "./chroma_db"
    chroma_collection: str = "toti_faq"
    knowledge_base_dir: str = "./knowledge_base/faq"
    rag_chunk_size: int = 512
    rag_chunk_overlap: int = 77  # ~15% of chunk_size
    rag_top_k: int = 3
    # Scope guard: retrieval similarity below this => out-of-scope, refuse to
    # answer from the LLM's general knowledge. Start reasonable, tune later.
    rag_similarity_threshold: float = 0.40

    # ── WhatsApp gateway (wwebjs-api) ─────────────────────────────────────────
    wwebjs_base_url: str = "http://wwebjs-api:3000"
    # No default on purpose: this key protects the logged-in WhatsApp session
    # (send as the store, read chats, export the session). Required at startup.
    wwebjs_api_key: str = ""
    wwebjs_session_id: str = "toti"

    # ── Local chatbot DB (SQLite) ─────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./toti_chatbot.db"
    # Personal data retention: transcripts older than this are deleted, and the
    # identity blob (nama/alamat/nomor HP) on finished orders is cleared. The
    # local DB is a plain file on the host — don't keep an address book in it
    # forever. 0 disables the purge.
    data_retention_days: int = 90

    # ── Payment tracking ──────────────────────────────────────────────────────
    # (Charging happens in the backend -> Midtrans; we only poll status here.)
    payment_check_interval_seconds: int = 30
    payment_timeout_minutes: int = 30
    # Decision: support full payment OR 50% down-payment (DP).
    allow_down_payment: bool = True
    down_payment_percentage: float = 0.50

    # ── Checkout / identity ───────────────────────────────────────────────────
    # Decision: phone auto-fills from the sender's WhatsApp number, overridable.
    autofill_phone_from_wa: bool = True

    # ── Verifikasi nomor WhatsApp (pendaftaran Buyer Site) ───────────────────
    # Pelanggan menekan tombol di Buyer Site -> WhatsApp terbuka dengan pesan
    # "VERIFIKASI <kode>" -> chatbot meneruskan {kode, nomor pengirim} ke backend.
    # Default MATI: endpointnya belum dibangun backend, dan 404 "route tidak
    # ada" tidak bisa dibedakan dari 404 "kode tidak dikenal" — pelanggan akan
    # dapat balasan membingungkan. Nyalakan (true) begitu backend siap; selama
    # mati, pesan verifikasi diperlakukan sebagai percakapan biasa.
    wa_verification_enabled: bool = False
    wa_verification_keyword: str = "VERIFIKASI"
    # Rem brute-force di sisi kita; backend tetap punya batasnya sendiri.
    wa_verification_max_per_hour: int = 10

    # ── Admin / human takeover ────────────────────────────────────────────────
    # Decision: single fixed admin number for now.
    admin_wa_number: str = ""
    takeover_expiry_days: int = 7

    # ── Store info (used in "ready for pickup/delivery" messages) ─────────────
    store_name: str = "Toti Cakery"
    store_address: str = "Jl. Contoh No. 123, Jakarta (ganti di .env)"

    # ── Owner gating for financial_report / business_analytics ────────────────
    owner_wa_numbers: str = ""  # comma-separated

    @property
    def owner_wa_list(self) -> list[str]:
        return [n.strip() for n in self.owner_wa_numbers.split(",") if n.strip()]

    def validate_runtime(self) -> None:
        """Fail fast on secrets that must never fall back to a default.

        Called from main.lifespan, not at import time, so tests and one-off
        scripts (ingest.py, chat_cli.py) don't need the production secrets.
        """
        weak = {"", "change-me", "changeme", "secret", "token"}
        missing = [
            name
            for name, value in (
                ("WEBHOOK_TOKEN", self.webhook_token),
                ("INTERNAL_API_KEY", self.internal_api_key),
                ("WWEBJS_API_KEY", self.wwebjs_api_key),
            )
            if value.strip().lower() in weak
        ]
        if self.allow_down_payment and self.down_payment_percentage != 0.50:
            # The backend recomputes the expected DP as exactly total * 0.5 and
            # rejects anything else with 400 — a different percentage here would
            # fail every DP checkout at charge time, silently until a real order.
            logging.getLogger(__name__).warning(
                "DOWN_PAYMENT_PERCENTAGE=%s but the backend validates DP against "
                "exactly 50%% — every DP checkout will be rejected with 400.",
                self.down_payment_percentage,
            )
        if missing:
            raise RuntimeError(
                "Refusing to start — these secrets are unset or left at a known "
                f"placeholder: {', '.join(missing)}. Generate them with "
                "`openssl rand -hex 24` and put them in .env "
                "(WEBHOOK_TOKEN must also match BASE_WEBHOOK_URL in docker-compose.yml)."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
