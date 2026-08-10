# Toti Cakery — WhatsApp Chatbot Service

RAG + tool-calling WhatsApp chatbot for Toti Cakery (a bakery). Built with FastAPI,
LangChain, Ollama (`qwen3:1.7b`), ChromaDB, and the `avoylenko/wwebjs-api` WhatsApp
gateway. This repo is **only** the chatbot + WhatsApp integration — the main backend
(`Backend-Cakery/`, reference-only) and the React frontends are owned by teammates.

> Scope, rules, and the full conversation flow live in
> `PROMPT_CLAUDE_CODE_TOTI_CAKERY_CHATBOT.md`. Endpoints the backend still owes us
> are in `BACKEND_TODO.txt`. See `CLAUDE.md` for an orientation aimed at AI agents.
>
> **Backend engineer?** Langsung ke
> [Konfigurasi backend ⇄ chatbot](#konfigurasi-backend--chatbot-untuk-backend-engineer).

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

1. **Docker + Docker Compose**.
2. **Models present in Ollama** (the compose `ollama` container mounts the host's
   `/usr/share/ollama/.ollama`, so models already pulled on the host are reused):
   ```bash
   ollama pull qwen3:1.7b            # base; LLM_MODEL is the fine-tune toti-qwen-1.7b
   ollama pull qwen3-embedding:0.6b
   ```
3. `Backend-Cakery/.env` present (the backend container reads it: `DATABASE_URL`,
   Midtrans keys, `SERVICE_API_KEY`). The chatbot degrades gracefully if the
   backend is down — tools reply "sedang tidak bisa diambil".

## Setup

```bash
cp .env.example .env          # then edit values (ADMIN_WA_NUMBER, BACKEND_SERVICE_API_KEY, ...)
docker compose up --build -d
```

This starts four containers: `chatbot-service` (:8000), `backend` (:8001),
`wwebjs-api` (:3000) — all bound to `127.0.0.1` — plus `ollama` (no published
port). Chroma runs embedded inside chatbot-service. Inter-container addresses
(`BACKEND_BASE_URL`, `OLLAMA_BASE_URL`, `WWEBJS_BASE_URL`) are overridden in
`docker-compose.yml`, so the `.env` values only matter when you run outside Docker.

### 1. Ingest the knowledge base (FAQ → ChromaDB)

```bash
docker compose exec chatbot-service python knowledge_base/ingest.py
# or locally:  cd chatbot-service && python knowledge_base/ingest.py
```
Re-run anytime — it's idempotent (re-embeds changed files, drops deleted ones).

### 2. Link WhatsApp (one-time, manual)

```bash
# start the session
curl http://localhost:3000/session/start/toti -H "x-api-key: $WWEBJS_API_KEY"
# open the QR image in a browser and scan it from WhatsApp on your phone
xdg-open "http://localhost:3000/session/qr/toti/image?x-api-key=$WWEBJS_API_KEY"
```
The session id (`toti`) comes from `WWEBJS_SESSION_ID`. Auth persists in
`whatsapp-gateway/sessions/` so you won't need to re-scan after restarts (that
folder is a live account credential — it's gitignored, keep it out of the repo).

---

## Konfigurasi backend ⇄ chatbot (untuk backend engineer)

> Bagian ini sengaja ditulis dalam bahasa Indonesia karena ditujukan ke backend
> engineer. Ringkasnya cuma **dua hal** yang perlu di-set di sisi backend:
> `CHATBOT_URL` dan `SERVICE_API_KEY`. Selebihnya sudah jalan otomatis.

### 1. `CHATBOT_URL` — isinya apa?

**Base URL saja, tanpa path dan tanpa trailing slash.** Path-nya sudah disusun di
kode push backend (`{CHATBOT_URL}/webhook/internal/orders/{id}/ready`).

| Backend jalan di mana | Isi `CHATBOT_URL` |
|---|---|
| Lewat `docker compose up` di repo gabungan (service `backend`) | `http://chatbot-service:8000` — **sudah otomatis di-set** di `docker-compose.yml`, tidak perlu ditambah ke `.env` |
| `uvicorn` langsung di mesin yang sama dengan chatbot | `http://localhost:8000` |
| Server/VPS lain | ❌ belum bisa — lihat catatan di bawah |

> **Catatan penting:** Chatbot Service **belum di-deploy** ke server mana pun, dan
> port 8000-nya sengaja di-bind ke `127.0.0.1` saja (bukan `0.0.0.0`) karena
> endpoint `/webhook/*` belum punya autentikasi — jadi tidak boleh terbuka ke
> LAN/internet. Backend yang jalan di mesin **lain** akan kena `Connection refused`.
> Begitu chatbot dideploy, cukup ganti nilai `CHATBOT_URL`; tidak ada perubahan kode.

`.env` backend:

```env
CHATBOT_URL=http://localhost:8000
```

### 2. Endpoint chatbot yang dipanggil backend

Cuma **satu**, yaitu push C4 "pesanan siap":

```
POST {CHATBOT_URL}/webhook/internal/orders/{order_id}/ready
```

| Hal | Keterangan |
|---|---|
| Kapan dipanggil | Saat admin menandai order jadi siap (status → *siap/ready*) |
| `order_id` | ID order dari database backend (`orders.id`) — sama dengan yang dikembalikan `POST /orders` |
| Body | **Tidak perlu** (boleh kosong). Chatbot mengambil semua data dari `order_id` |
| Header auth | **Tidak perlu**. `X-Service-Key` itu untuk arah sebaliknya (chatbot → backend) |
| Response 200 | `{"status": "ok", "order_id": 12}` → pesan WA sudah dikirim ke pelanggan |
| Response 200 | `{"status": "not_found", "order_id": 12}` → order-nya tidak ada di sisi chatbot (mis. dibuat lewat website, bukan lewat WA). **Bukan error**, tidak usah di-retry terus |
| Idempoten | Ya. Dipanggil berulang untuk order yang sama → pelanggan tetap hanya dikirimi pesan sekali |

Panggilannya sebaiknya *fire-and-forget* — jangan sampai update status di backend
gagal cuma karena chatbot lagi mati. Cukup `try/except` + log.

Cek cepat:

```bash
curl http://localhost:8000/health
# -> {"status":"ok","service":"Toti Cakery Chatbot Service"}

curl -X POST http://localhost:8000/webhook/internal/orders/12/ready
# -> {"status":"ok","order_id":12}   (atau "not_found" kalau order-nya bukan dari WA)
```

### 3. `SERVICE_API_KEY` — harus cocok dua arah

Semua panggilan chatbot → backend mengirim header `X-Service-Key: <SERVICE_API_KEY>`.
Di sisi chatbot nilainya ada di `.env` sebagai `BACKEND_SERVICE_API_KEY`. **Dua-duanya
harus sama persis**; kalau beda, semua fitur transaksi bot (buat order, bayar, cek
status, laporan Owner) balas `401`. Nilai production disepakati privat — jangan
di-commit ke repo mana pun.

### 4. Endpoint backend yang dipakai chatbot (kontrak beku)

Jangan ubah path/bentuk response-nya tanpa kabar-kabari — kalau harus berubah,
bilang dulu supaya chatbot diupdate barengan.

| Method | Path | Dipakai untuk |
|---|---|---|
| `GET` | `/products/` | Tool `get_menu` (daftar menu, live — tidak pernah di-cache) |
| `GET` | `/products/{id}` | Detail produk + `image_url` |
| `POST` | `/customers` | Simpan/ambil data pelanggan (`nomor_wa`, `nama`, `alamat`) |
| `POST` | `/orders` | Buat pesanan (`customer_id`, `metode_pengiriman`, `created_via`, `items[{product_id, jumlah}]`) |
| `GET` | `/orders/latest?nomor_wa=` | Cek status pesanan terakhir (nomor format lengkap `628…@c.us`) |
| `POST` | `/orders/{id}/cancel` | Batalkan pesanan (stok dikembalikan) |
| `POST` | `/payments` | Charge Midtrans (`{order_id, payment_type: bank_transfer\|qris, amount}`) |
| `GET` | `/payments/{order_id}/status` | Polling pembayaran tiap `PAYMENT_CHECK_INTERVAL_SECONDS` |
| `GET` `POST` | `/customers/{nomor_wa}/takeover` | Human takeover (baca & set) |
| `GET` | `/admin/takeover-handlers` | Daftar nomor admin → `{"numbers": [...]}` |
| `GET` | `/reports/summary?start_date&end_date` | Laporan Owner lewat WA |

Bentuk response yang diandalkan:

- `CustomerOut` / `OrderOut` pakai field `id`; nomor invoice ada di `OrderOut.invoice.nomor_invoice`
- `GET /payments/{order_id}/status` → `{order_id, invoice_status, amount_paid, amount_due, payments[]}`
- `GET /reports/summary` → `{revenue, expenses, order_count, avg_order_value, top_products[{product_id, nama_produk, qty, revenue}]}`

**Chatbot tidak pernah memanggil Midtrans langsung** — semua charge lewat
`POST /payments` di backend; chatbot cuma meneruskan VA/QRIS ke pelanggan dan
polling statusnya.

### 5. Foto produk: simpan **path relatif**

Kolom `products.image_url` diisi path saja: `/static/products/12.jpg`. Jangan URL
absolut (`http://localhost:8001/...`) — alamat host beda-beda tergantung pemanggilnya
(browser vs container chatbot vs production). Tiap konsumen mem-prefix base URL-nya
sendiri; di chatbot sudah diimplementasi (`BACKEND_BASE_URL` + path).

### 6. Kalau ada yang aneh

| Gejala | Kemungkinan sebab |
|---|---|
| Push ready → `Connection refused` | Chatbot tidak jalan, atau backend ada di mesin lain (chatbot cuma listen di `127.0.0.1`) |
| Push ready → `404 Not Found` | Salah path. Harus ada prefix `/webhook`: `/webhook/internal/orders/{id}/ready` |
| Push ready → `{"status":"not_found"}` | Bukan error transport. Order-nya memang bukan pesanan dari WhatsApp |
| Chatbot dapat `401` dari backend | `SERVICE_API_KEY` vs `BACKEND_SERVICE_API_KEY` beda |
| Dari dalam container tidak bisa connect ke `localhost` | Di dalam Docker, `localhost` = container itu sendiri. Pakai nama service: `chatbot-service`, `backend`, `ollama` |

Daftar to-do backend lama + status verifikasinya ada di `BACKEND_TODO.txt`.

---

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
