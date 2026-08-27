# Toti Cakery — WhatsApp Chatbot Service

RAG + tool-calling WhatsApp chatbot for Toti Cakery (a bakery). Built with FastAPI,
LangChain, Ollama (`toti-qwen-1.7b-v4`), ChromaDB, and the `avoylenko/wwebjs-api` WhatsApp
gateway. This repo is **only** the chatbot + WhatsApp integration — the main backend
(deployed at `https://backend-cakery.vercel.app`) and the React frontends are owned by teammates.

> **Deploying to a VPS?** `DEPLOY_VPS.md` is the one to follow — there the whole
> stack (chatbot + backend + PostgreSQL) runs as containers on your own box.
> This README covers the laptop setup, where the backend is the Vercel deployment.

> Scope, rules, and the full conversation flow live in
> `PROMPT_CLAUDE_CODE_TOTI_CAKERY_CHATBOT.md`. Endpoints the backend still owes us
> are in `BACKEND_TODO.txt`. What the **backend** side has to configure to talk to
> this service (its `CHATBOT_URL`, the shared service key, the ready-push contract)
> lives in `BACKEND.md`. See `CLAUDE.md` for an orientation aimed at AI agents.

## Architecture

```
Customer (WhatsApp)
   ▼
wwebjs-api (Docker) ──webhook──▶ chatbot-service /webhook/whatsapp/$WEBHOOK_TOKEN
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
| **The fine-tuned GGUF** | `toti-qwen-1.7b-v4` isn't on the Ollama registry. `scripts/bootstrap.sh` builds it into the `ollama_models` Docker volume from `finetune/*.gguf.v4`, or downloads it from the private HF repo when `HF_TOKEN` is set. No host Ollama needed |
| **~8 GB free RAM** | Measured: the LLM takes **5.2 GB** at the default `LLM_NUM_CTX=32768` (**2.4 GB** at 8192) and embeddings **1.4 GB**, both kept resident by `OLLAMA_KEEP_ALIVE=-1`; the other four containers add ~1.5 GB. CPU-only inference works; a reply takes a few seconds |
| **A spare WhatsApp number** | Linking scans a QR from *WhatsApp → Linked devices*. Use a number you don't mind having a bot on |
| **`BACKEND_SERVICE_API_KEY`** | Must equal the backend's `SERVICE_API_KEY` (ask the backend engineer). The chatbot still starts without a reachable backend — product/order tools just reply "sedang tidak bisa diambil" |

## Setup — step by step

Every command is run from the repo root unless stated otherwise.

### 1. Build the models into the Ollama volume

Models live in a Docker volume (`ollama_models`), not in a host Ollama install —
so this step is the same on a laptop and on a VPS. `scripts/bootstrap.sh` (step 3)
does it for you; run it by hand only if you want the model ready first:

```bash
docker compose up -d ollama
docker compose exec ollama ollama pull qwen3-embedding:0.6b   # embeddings for RAG
docker compose exec ollama ollama list                        # verify
```

`LLM_MODEL` defaults to **`toti-qwen-1.7b-v4`** — the fine-tuned model, not the base.
Do not fall back to v3 (`toti-qwen-1.7b`): tested live against the current catalogue
it invents product names ("Brownies 10cm Cokelat") and answers menu questions
without calling `get_menu`. Building it needs the GGUF — put
`finetune/toti-qwen-1.7b.Q4_K_M.gguf.v4` in place (or set `HF_TOKEN`) and let
bootstrap run `ollama create`. If you'd rather run the plain base model for now,
set `LLM_MODEL=qwen3:1.7b` in `.env` (quality on tool-calling will be noticeably worse).

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
| `LLM_MODEL` | `toti-qwen-1.7b-v4` (see step 1) |

`OLLAMA_BASE_URL` and `WWEBJS_BASE_URL` are **overridden in `docker-compose.yml`**
with container names, so their `.env` values only matter when you run the service
outside Docker (see the last section). `BACKEND_BASE_URL` is **not** overridden —
the backend is the Vercel deployment, same URL from inside and outside Docker.

### 3. Start the stack

```bash
docker compose up --build -d
docker compose ps          # all three should be "running"
```

Five containers come up:

| Container | Port | Notes |
|---|---|---|
| `toti-chatbot` | `127.0.0.1:8000` | This service. Localhost-only as defence in depth; `/webhook/*` is authenticated too |
| `toti-backend` | `127.0.0.1:8001` | The teammate's FastAPI, built from a pinned commit of `Nicholl2/Backend-Cakery` (`BACKEND_REF`). We never edit it — only build it |
| `toti-postgres` | — | Backend's database. Schema is auto-created on start; **no seed data** — see `DEPLOY_VPS.md` §7 |
| `toti-wwebjs` | `127.0.0.1:3000` | WhatsApp gateway |
| `toti-ollama` | — | No published port; only reachable inside the compose network |

Chroma is **not** a container — it runs embedded inside chatbot-service and persists
to `chatbot-service/chroma_db/`.

First boot is slow: chatbot-service preloads both models (`WARMUP_ON_STARTUP=true`, ~1 min on CPU).
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

# 2. save the QR and scan it: WhatsApp → Settings → Linked devices → Link a device
#    (the key MUST go in the header — this gateway rejects ?x-api-key= with 403)
curl -H "x-api-key: $WWEBJS_API_KEY" "http://localhost:3000/session/qr/toti/image" -o qr.png && xdg-open qr.png

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
| `model "…" not found` in the logs | `LLM_MODEL` isn't in the volume: `docker compose exec ollama ollama list` — re-run `./scripts/bootstrap.sh` |
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
  curl -X POST http://localhost:8000/webhook/internal/orders/<id>/ready \
       -H "X-Internal-Key: $INTERNAL_API_KEY"
  ```
- **End human takeover (manual):**
  ```bash
  curl -X POST http://localhost:8000/webhook/internal/takeover/<phone>/deactivate \
       -H "X-Internal-Key: $INTERNAL_API_KEY"
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
$(grep -E '^(WEBHOOK_TOKEN|INTERNAL_API_KEY|WWEBJS_API_KEY)=' ../.env | xargs) \
uvicorn app.main:app --reload --port 8000
```

The service **refuses to start** without `WEBHOOK_TOKEN`, `INTERNAL_API_KEY` and
`WWEBJS_API_KEY` (they authenticate the webhook, the internal endpoints, and the
WhatsApp session) — hence the `grep` line above, which pulls them out of the root
`.env` that `uvicorn` doesn't read from `chatbot-service/`. The error message on
startup tells you which one is missing.

Same applies to any script you run on the host (`ingest.py`, `scripts/chat_cli.py`,
the fine-tune eval harness). If wwebjs-api is still running in Docker, point its
`BASE_WEBHOOK_URL` at `http://host.docker.internal:8000/webhook/whatsapp/$WEBHOOK_TOKEN` (or just
use the CLI in *Testing it* and skip WhatsApp entirely).

## Key configuration (`.env`)

| Var | Meaning |
|---|---|
| `WEBHOOK_TOKEN` | **Required.** Secret path segment that authenticates wwebjs-api's callbacks (`/webhook/whatsapp/<token>`) — the gateway can't send headers. Without it anyone reaching the service can forge a message from any customer's number. `openssl rand -hex 24` |
| `INTERNAL_API_KEY` | **Required.** `X-Internal-Key` for `/webhook/internal/*` (end takeover, push "pesanan siap"). Backend must send it too — see `BACKEND.md` |
| `WWEBJS_API_KEY` | **Required.** Protects the logged-in WhatsApp session. No default — the service refuses to start on a placeholder |
| `LOG_MESSAGE_BODIES` | Log customer message text (default `false`; metadata is logged either way, phone numbers masked) |
| `DATA_RETENTION_DAYS` | Transcripts older than this are purged and identity snapshots on finished orders cleared (default 90, `0` = off) |
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
