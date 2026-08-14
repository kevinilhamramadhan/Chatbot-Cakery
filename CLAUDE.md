# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this workspace is

This is Kevin's working directory for the **Toti Cakery WhatsApp Chatbot** (a 3-person capstone; Kevin owns the Chatbot Service + WhatsApp integration only). Two things matter:

1. **`PROMPT_CLAUDE_CODE_TOTI_CAKERY_CHATBOT.md` is the authoritative spec.** Read it before doing chatbot work. It defines the full scope, rules, architecture, conversation flow, and a list of open questions (its section 17) that must be confirmed with Kevin before implementing the affected parts.
2. **The chatbot service is built and fully wired to the real backend** (`chatbot-service/`, `whatsapp-gateway/`, `docker-compose.yml`). The spec's folder layout (spec §3) predates some cleanups: the mock `services/payment-gateway/` was deleted once the real Midtrans flow moved into the main backend.

## The backend is a remote service — do not modify it

The teammate's FastAPI + PostgreSQL backend runs at **`https://backend-cakery.vercel.app`** (`BACKEND_BASE_URL`). Its source lives in its own repo (github.com/Nicholl2/Backend-Cakery) and is **no longer checked out in this workspace** — the chatbot talks to the deployment over HTTP only. **Never edit the backend, and never add endpoints to it.** Building chatbot code is the entire job here.

Need to read backend source (schemas, route auth)? Clone it somewhere outside this workspace, or read `{BACKEND_BASE_URL}/openapi.json`.

### Historical gotcha (fixed): double-prefix routing bug in Backend-Cakery

Early versions of the backend doubled every path (router `prefix=` + another prefix in `app/main.py`, e.g. `/products/products/`). The backend owner fixed this — live paths are now the clean ones (`/products/`, `/faq`, `/customers`, `/orders`, …) and the chatbot calls them directly. If a backend path ever 404s unexpectedly, check `{BACKEND_BASE_URL}/openapi.json` first before assuming a chatbot bug; the base URL stays in config (`BACKEND_BASE_URL`), never hardcoded.

### Backend endpoints: real vs. not-yet-built

- **Usable now** (buyer-relevant): products list/detail, FAQ list/detail, stock-items. `get_menu` / `get_product_detail` tools wire to these — `get_menu` is the one tool whose backend endpoint definitely exists, so it's the end-to-end smoke test (spec §15.6).
- **All backend endpoints the chatbot needs are built and live-verified** (backend commit `8670242`): customers, orders, payments/Midtrans, takeover, takeover-handlers, `is_available`, ready-push, order-status update, and `GET /reports/summary` (Owner reports — wired into `financial_report`/`business_analytics` for real). Do **not** invent endpoints beyond these; anything new the backend must build goes in `BACKEND_TODO.txt` (name, method+path, request/response shape, why), which also lists the frozen response contracts the chatbot depends on. See spec §1, §5.

Product/FAQ response shapes are documented in `{BACKEND_BASE_URL}/openapi.json` (note Indonesian field names: `nama_produk`, `harga_jual`, `deskripsi`, `kategori`, `minimum_order`, `pertanyaan`, `jawaban`).

## Hard rules from the spec (don't violate)

- **The chatbot never talks to Midtrans directly** — charging happens in the main backend (`POST /payments`); the chatbot only relays the VA/QRIS it gets back and polls payment status. Never install `midtransclient` in the chatbot, never put Midtrans credentials anywhere in this service.
- **RAG scope guard**: if retrieval similarity is below the configurable threshold, the bot must refuse to answer from general LLM knowledge and reply that it only serves Toti Cakery topics (spec §7). Threshold is a named config var, never hardcoded.
- **Product data is not embedded into Chroma** — only FAQ files are. Product info is fetched live via tools so prices/stock don't go stale (spec §7).
- The chatbot's own SQLite DB (sessions, cart drafts, conversation log, pending orders) is internal storage — that is *not* "adding a backend endpoint" and is allowed (spec §14).
- Build iteratively: skeleton + WhatsApp echo first, then RAG, then tools one at a time (spec §1.6). Don't generate everything in one giant file.

## Planned stack (fixed — see spec §2)

Python 3.11+ / FastAPI, LangChain, Ollama (`qwen3:1.7b` LLM + `qwen3-embedding:0.6b`), ChromaDB (persistent), SQLite via SQLAlchemy, `avoylenko/wwebjs-api` (Node, Docker) as the WhatsApp gateway (configure it, never rewrite it). Whole stack runs via `docker-compose`.

## Commands

The chatbot service has no code yet, so it has no build/test commands. Once scaffolded, the spec expects:

```bash
docker-compose up                       # whole stack: chatbot-service, chroma, wwebjs-api (+ollama if needed)
python chatbot-service/knowledge_base/ingest.py   # (re)embed FAQ files into ChromaDB (idempotent)
```

Verifying real backend endpoints:

```bash
curl https://backend-cakery.vercel.app/openapi.json | jq '.paths | keys'
# Swagger UI: https://backend-cakery.vercel.app/docs
```
