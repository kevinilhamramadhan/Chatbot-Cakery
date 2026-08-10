"""Central configuration for the Toti Cakery chatbot service.

Every tunable lives here and is sourced from environment variables so nothing
operational is hardcoded in business logic (see PROMPT rules #2 and #4).
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── Service ───────────────────────────────────────────────────────────────
    app_name: str = "Toti Cakery Chatbot Service"
    log_level: str = "INFO"

    # ── Backend (teammate's FastAPI) ──────────────────────────────────────────
    backend_base_url: str = "http://localhost:8001"
    backend_request_timeout_seconds: float = 10.0
    # Sent as X-Service-Key on every backend call. Must match the backend's
    # SERVICE_API_KEY (require_service_key dependency). Harmless on public GETs.
    backend_service_api_key: str = ""

    # ── Ollama (LLM + embeddings) ─────────────────────────────────────────────
    ollama_base_url: str = "http://localhost:11434"
    llm_model: str = "qwen3.5:0.8b"
    embedding_model: str = "qwen3-embedding:0.6b"
    llm_temperature: float = 0.7
    llm_top_p: float = 0.8
    llm_num_ctx: int = 32768
    # Hard cap on generated tokens per reply. Without it, Ollama defaults to
    # unlimited and a small model that fails to stop cleanly can generate for
    # 100s+ on CPU (observed on 1.7b). 768 fits a thinking trace + a WA reply.
    llm_num_predict: int = 768
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
    wwebjs_api_key: str = "change-me"
    wwebjs_session_id: str = "toti"

    # ── Local chatbot DB (SQLite) ─────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./toti_chatbot.db"

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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
