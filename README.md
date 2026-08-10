# Toti Cakery — WhatsApp Chatbot Service

RAG + tool-calling WhatsApp chatbot for Toti Cakery (a bakery). Built with FastAPI,
LangChain, Ollama (`qwen3:1.7b`), ChromaDB, and the `avoylenko/wwebjs-api` WhatsApp
gateway. This repo is **only** the chatbot + WhatsApp integration — the main backend
(`Backend-Cakery/`, reference-only) and the React frontends are owned by teammates.

> Scope, rules, and the full conversation flow live in
> `PROMPT_CLAUDE_CODE_TOTI_CAKERY_CHATBOT.md`. Endpoints the backend still owes us
> are in `BACKEND_TODO.txt`. What the **backend** side has to configure to talk to
> this service (its `CHATBOT_URL`, the shared service key, the ready-push contract)
> lives in `BACKEND.md`. See `CLAUDE.md` for an orientation aimed at AI agents.

## Architecture

```
Customer (WhatsApp)
   ▼
wwebjs-api (Docker) ──webhook──▶ chatbot-service /webhook/whatsapp
                                       │  orchestrator (state machine)
              ┌────────────────────────┼─────────────────────────┐
              ▼                         ▼                          ▼
        rag/ (ChromaDB +          tools/ (LangChain          backend_client/
        qwen3-embedding)          tool calling)              (real HTTP -> backend:
              │                         │                     products, orders,
              ▼                         ▼                     customers, payments,
        llm/ (Ollama, tool calling)                           takeover, reports)
```

- **Everything is real**: products, orders, customers, payments (Midtrans via the
  backend), human takeover, ready-push, and Owner reports all hit the main
  backend (Neon PostgreSQL). No mocks remain.
- The chatbot keeps its **own SQLite DB** (sessions, conversation log, pending orders).

## Prerequisites

| Need | Why / notes |
|---|---|
| **Docker + Docker Compose v2** | The whole stack runs as containers. `docker compose version` should print v2.x |
| **Ollama installed on the host** | The `ollama` container mounts the host's model store (`/usr/share/ollama/.ollama`), so models you already pulled are reused instead of re-downloaded |
| **~6 GB free RAM** | `toti-qwen-1.7b` + `qwen3-embedding:0.6b` stay resident (`OLLAMA_KEEP_ALIVE=-1`). CPU-only inference works; a reply takes a few seconds |
| **A spare WhatsApp number** | Linking scans a QR from *WhatsApp → Linked devices*. Use a number you don't mind having a bot on |
| **`Backend-Cakery/.env`** | The backend container reads it (`DATABASE_URL`, Midtrans keys, `SERVICE_API_KEY`). Ask the backend engineer for it. The chatbot still starts without a working backend — product/order tools just reply "sedang tidak bisa diambil" |

## Setup — step by step

Every command is run from the repo root unless stated otherwise.

### 1. Pull the models into Ollama

```bash
ollama pull qwen3:1.7b            # base model
ollama pull qwen3-embedding:0.6b  # embeddings for RAG (must match EMBEDDING_MODEL)
ollama list                       # verify both appear
```

`LLM_MODEL` defaults to **`toti-qwen-1.7b`** — the fine-tuned model, not the base.
Build it once from the GGUF + Modelfile as described in `finetune/README.md`, then
confirm with `ollama list`. If you'd rather run the plain base model for now, set
`LLM_MODEL=qwen3:1.7b` in `.env` (quality on tool-calling will be noticeably worse).

### 2. Create `.env`

```bash
cp .env.example .env
```

Then edit the values that are *not* safe to leave at their defaults:

| Var | Set it to |
|---|---|
| `BACKEND_SERVICE_API_KEY` | The backend's `SERVICE_API_KEY`, character-for-character. Mismatch ⇒ every transactional tool gets `401` |
| `WWEBJS_API_KEY` | Any random string; it must match what the gateway container gets (compose reads the same `.env`) |
| `ADMIN_WA_NUMBER` | The admin's number in `628…` form — receives human-takeover escalations |
| `OWNER_WA_NUMBERS` | Comma-separated `628…` numbers allowed to ask for financial reports |
| `STORE_NAME` / `STORE_ADDRESS` | Real store name + address; they're pasted into "your order is ready" messages |
| `LLM_MODEL` | `toti-qwen-1.7b` (see step 1) |

`BACKEND_BASE_URL`, `OLLAMA_BASE_URL`, and `WWEBJS_BASE_URL` are **overridden in
`docker-compose.yml`** with container names, so their `.env` values only matter when
you run the service outside Docker (see the last section).

### 3. Start the stack

```bash
docker compose up --build -d
docker compose ps          # all four should be "running"
```

Four containers come up:

| Container | Port | Notes |
|---|---|---|
| `toti-chatbot` | `127.0.0.1:8000` | This service. Localhost-only on purpose — `/webhook/*` has no auth |
| `cakery-backend` | `127.0.0.1:8001` | The teammate's FastAPI, Swagger at `/docs` |
| `toti-wwebjs` | `127.0.0.1:3000` | WhatsApp gateway |
| `toti-ollama` | — | No published port; only reachable inside the compose network |

Chroma is **not** a container — it runs embedded inside chatbot-service and persists
to `chatbot-service/chroma_db/`.

First boot is slow: the backend container installs its requirements, and
chatbot-service preloads both models (`WARMUP_ON_STARTUP=true`, ~1 min on CPU).
Watch it finish with:

```bash
docker compose logs -f chatbot-service
# wait for: Uvicorn running on http://0.0.0.0:8000
curl http://localhost:8000/health
# -> {"status":"ok","service":"Toti Cakery Chatbot Service"}
```

### 4. Ingest the knowledge base (FAQ → ChromaDB)

The RAG store starts empty — without this step the bot refuses every FAQ question
via the scope guard.

```bash
docker compose exec chatbot-service python knowledge_base/ingest.py
# or outside Docker:  cd chatbot-service && python knowledge_base/ingest.py
```

Source files are `chatbot-service/knowledge_base/faq/*.txt` (one topic per file).
Re-run anytime — it's idempotent: changed files are re-embedded and deleted files
drop their vectors. Re-run it **whenever you edit a FAQ file**.

### 5. Link WhatsApp (one-time, manual)

```bash
# 1. start the session (session id comes from WWEBJS_SESSION_ID, default "toti")
curl "http://localhost:3000/session/start/toti" -H "x-api-key: $WWEBJS_API_KEY"

# 2. open the QR and scan it: WhatsApp → Settings → Linked devices → Link a device
xdg-open "http://localhost:3000/session/qr/toti/image?x-api-key=$WWEBJS_API_KEY"

# 3. confirm the pairing worked
curl "http://localhost:3000/session/status/toti" -H "x-api-key: $WWEBJS_API_KEY"
# -> {"success":true,"state":"CONNECTED","message":"session_connected"}
```

The QR expires after ~20 seconds — refresh the image if the scan misses it. If
scanning is awkward, the gateway also offers a pairing code instead:
`POST /session/requestPairingCode/{sessionId}`.

Auth persists in `whatsapp-gateway/sessions/`, so restarts don't need a re-scan.
That folder is a **live account credential**: it's gitignored, keep it that way.
To recover a stuck session: `/session/restart/toti`, or `/session/terminate/toti`
followed by step 1 for a clean re-link.

### 6. Smoke-test the whole path

From another phone, message the bot's number:

```
menu apa aja
```

A correct reply lists live products and prices — that means WhatsApp → gateway →
webhook → LLM → `get_menu` tool → backend all worked. Follow the logs while you do it:

```bash
docker compose logs -f chatbot-service   # "WA in <- …", tool calls, "WA out -> …"
```

### Day-to-day commands

```bash
docker compose restart chatbot-service   # after changing .env
docker compose up -d --build chatbot-service   # after changing chatbot code
docker compose down                      # stop everything (sessions + data persist)
docker compose logs -f --tail=100 chatbot-service
```

### Setup troubleshooting

| Symptom | Cause / fix |
|---|---|
| `curl localhost:8000/health` refused | Container still warming up (models preloading) — check `docker compose logs chatbot-service` |
| Bot silent on WhatsApp | Session not `CONNECTED` (step 5), or `WWEBJS_API_KEY` in `.env` ≠ the gateway's |
| Every FAQ answer is "di luar topik" | Step 4 never ran, or `EMBEDDING_MODEL` ≠ the model used at ingest time — re-ingest after changing it |
| Product/order tools say "sedang tidak bisa diambil" | Backend down, or `BACKEND_SERVICE_API_KEY` ≠ the backend's `SERVICE_API_KEY` (`401`) |
| `model "…" not found` in the logs | `LLM_MODEL` isn't in `ollama list` on the host (step 1) |
| Very slow first reply, then fast | Normal: cold model load. `OLLAMA_KEEP_ALIVE=-1` keeps it resident afterwards |

## Testing it

### Fastest: local CLI (no phone/WhatsApp needed)

Drives the full brain (RAG + tools + order flow) via the terminal; outbound WA
sends are stubbed. Needs Ollama running (and the backend, for real product tools):

```bash
cd chatbot-service
python knowledge_base/ingest.py        # once, to populate ChromaDB
python -m scripts.chat_cli             # then just chat; /state to inspect, /quit
```

### Full stack (real WhatsApp)

- **Smoke test (real tool):** message the bot **"menu apa aja"** → it calls the real
  `get_menu` and replies with live products + prices.
- **Order flow:** "mau pesan brownies 2" → confirm ("sudah sesuai") → give name &
  address → pickup/delivery → confirm phone → choose full/DP → receive VA + QR.
- **Simulate payment:** Midtrans is real (sandbox) — pay the VA/QRIS in the Midtrans
  sandbox simulator. The payment only flips to `Success` after Midtrans calls the
  backend's `POST /payments/notify`; within `PAYMENT_CHECK_INTERVAL_SECONDS` of that,
  the bot proactively confirms payment.
- **Mark order ready (proactive pickup/delivery msg):**
  ```bash
  curl -X POST http://localhost:8000/webhook/internal/orders/<id>/ready
  ```
- **End human takeover (manual):**
  ```bash
  curl -X POST http://localhost:8000/webhook/internal/takeover/<phone>/deactivate
  ```

## Unit tests

```bash
cd chatbot-service
pip install -r requirements.txt
pytest
```

## Running the service outside Docker (dev loop)

Useful when you're editing chatbot code and don't want a rebuild per change.
Python **3.11+**:

```bash
cd chatbot-service
python -m venv .venv && source .venv/bin/activate   # fish: source .venv/bin/activate.fish
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

⚠️ **The `.env` in this repo is written for Docker**, where hosts are container
names. Running on the host you must override the three addresses, otherwise the
service fails in confusing ways (Ollama calls hang / RAG silently returns nothing):

```bash
OLLAMA_BASE_URL=http://localhost:11434 \
BACKEND_BASE_URL=http://localhost:8001 \
WWEBJS_BASE_URL=http://localhost:3000 \
uvicorn app.main:app --reload --port 8000
```

Same applies to any script you run on the host (`ingest.py`, `scripts/chat_cli.py`,
the fine-tune eval harness). If wwebjs-api is still running in Docker, point its
`BASE_WEBHOOK_URL` at `http://host.docker.internal:8000/webhook/whatsapp` (or just
use the CLI in *Testing it* and skip WhatsApp entirely).

## Key configuration (`.env`)

| Var | Meaning |
|---|---|
| `BACKEND_BASE_URL` | Base URL of the main backend (paths resolved defensively) |
| `BACKEND_SERVICE_API_KEY` | Sent as `X-Service-Key`; must equal the backend's `SERVICE_API_KEY` |
| `OLLAMA_BASE_URL` | Ollama endpoint (LLM + embeddings) |
| `LLM_MODEL` | `toti-qwen-1.7b` (the fine-tune) — see `finetune/README.md` |
| `RAG_SIMILARITY_THRESHOLD` | Below this, the bot refuses out-of-topic questions (tune me) |
| `ADMIN_WA_NUMBER` | Single admin number for escalation notifications |
| `ALLOW_DOWN_PAYMENT` / `DOWN_PAYMENT_PERCENTAGE` | Enable DP 50% at checkout |
| `PAYMENT_TIMEOUT_MINUTES` / `PAYMENT_CHECK_INTERVAL_SECONDS` | Payment timeout + poll cadence |
| `STORE_NAME` / `STORE_ADDRESS` | Used in pickup/delivery messages |

## Decisions baked in (confirmed with Kevin)

- **Checkout phone** auto-fills from the sender's WhatsApp number (overridable).
- **Payment**: supports full payment **and** DP 50%.
- **Admin**: single fixed `ADMIN_WA_NUMBER`.
- **RAG threshold**: a single config var (`RAG_SIMILARITY_THRESHOLD`), not hardcoded.
