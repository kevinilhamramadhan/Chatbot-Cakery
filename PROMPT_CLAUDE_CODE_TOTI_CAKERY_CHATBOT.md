# Prompt untuk Claude Code — Toti Cakery WhatsApp Chatbot System

> Cara pakai: copy seluruh isi file ini sebagai pesan pertama ke Claude Code di root folder project baru (kosong). Biarkan Claude Code membaca semuanya dulu sebelum mulai coding.

---

## 0. KONTEKS PROYEK

Saya (Kevin) adalah AI/Chatbot Engineer di tim capstone 3 orang yang membangun **Toti Cakery** — sistem pemesanan & manajemen stok kue berbasis web dengan integrasi chatbot WhatsApp. Tugas saya: membangun **Chatbot Service** (RAG + Tool Calling) dan **integrasi WhatsApp**-nya saja. Teammate saya (backend, FastAPI + PostgreSQL) sudah membangun sebagian endpoint (akan saya lampirkan source code-nya di pesan berikutnya / folder `reference/backend-cakery/`), dan teammate satu lagi mengerjakan frontend React.

**Tugas kamu (Claude Code) di sesi ini HANYA**: membangun **Chatbot Service** + **integrasi WhatsApp**. Jangan menyentuh/membangun frontend (React Buyer Site / Admin Site), dan jangan menambah endpoint baru ke repo backend utama punya teammate saya — repo backend itu bukan tanggung jawabmu di sini.

---

## 1. ATURAN MAIN — WAJIB DIPATUHI

1. **Jangan pernah berasumsi sebuah backend endpoint ada** kecuali sudah eksplisit saya konfirmasi atau ada di daftar "Endpoint yang Sudah Tersedia" di bawah. Kalau di tengah implementasi kamu butuh endpoint yang tidak ada di daftar itu (misal endpoint untuk membuat order, simpan customer, dll), **JANGAN membuat endpoint itu sendiri dan jangan mengarang response palsu seakan-akan endpoint itu nyata**. Sebagai gantinya:
   - Buat implementasi **mock/placeholder** yang jelas-jelas ditandai `# MOCK — endpoint backend belum tersedia` di kode, mengembalikan data dummy yang realistis supaya alur percakapan tetap bisa dites end-to-end.
   - Tambahkan baris ke file `MISSING_ENDPOINTS.md` (lihat bagian 15) berisi: nama tool, method+path yang diasumsikan, request/response shape yang dibutuhkan, dan kenapa dibutuhkan.
   - Lanjutkan kerjaan lain, tidak perlu berhenti total — tapi laporkan ringkasan semua endpoint yang di-mock di akhir sesi kerja.
2. Karena saya sempat menemukan source backend punya bug double-prefix routing (lihat bagian 4), **jangan hardcode path API mentah-mentah dari hasil baca source code**. Selalu taruh base path di config/env (`BACKEND_BASE_URL`), dan kalau memungkinkan, verifikasi path asli lewat endpoint Swagger backend (`{BACKEND_BASE_URL}/docs` atau `/openapi.json`) saat backend itu sudah jalan — kalau belum jalan, pakai asumsi path "bersih" (tanpa double prefix) dan catat sebagai asumsi di `MISSING_ENDPOINTS.md` juga.
3. Tools untuk tool-calling **belum final** — daftar di bagian 9 adalah titik awal berdasarkan dokumen spesifikasi tim saya, boleh kamu sesuaikan strukturnya (nama function, parameter) asal behavior-nya tetap sama, tapi jangan diam-diam menghapus salah satu kemampuan inti tanpa bilang ke saya.
4. Integrasi Midtrans **HARUS** ditaruh di subfolder terpisah (`services/payment-gateway/`), terisolasi dari kode chatbot inti, dan **HANYA berupa mock/placeholder** untuk sekarang — implementasi real Midtrans akan dikerjakan teammate backend saya di repo dia sendiri nanti. Jangan install/pakai SDK Midtrans asli, jangan hit API Midtrans yang sungguhan.
5. Kalau ada bagian requirement di bawah yang ambigu atau saling bertentangan, **tanya saya dulu** sebelum mengasumsikan — terutama yang saya tandai ⚠️ ASUMSI PERLU KONFIRMASI.
6. Build secara **iteratif**: mulai dari skeleton arsitektur + WhatsApp gateway nyambung dulu (bisa kirim/terima pesan teks polos), baru RAG, baru tool calling satu-satu. Jangan coba generate semuanya sekaligus dalam satu file raksasa.

---

## 2. TECH STACK (FIXED, jangan diganti)

| Komponen | Teknologi |
|---|---|
| Bahasa & framework Chatbot Service | Python 3.11+, FastAPI (untuk expose webhook & internal endpoints) |
| Orkestrasi AI | LangChain |
| LLM | `qwen3:1.7b` via **Ollama** (lokal, `OLLAMA_BASE_URL`, default `http://localhost:11434`) |
| Embedding model | `qwen3-embedding:0.6b` via Ollama |
| Vector store | ChromaDB (lokal/persistent, bukan in-memory) |
| WhatsApp gateway | **wwebjs-api** (repo open-source `avoylenko/wwebjs-api`, Node.js) — dijalankan sebagai service Docker terpisah, JANGAN ditulis ulang, tinggal dikonfigurasi (docker-compose, env, webhook URL) |
| Session/state chatbot lokal | SQLite (cukup untuk skala capstone; gunakan SQLAlchemy biar gampang migrasi ke Postgres kalau perlu nanti) |
| Container | Docker + docker-compose untuk seluruh stack (chatbot service, ollama jika belum jalan, chroma, wwebjs-api) |

---

## 3. STRUKTUR FOLDER YANG DIHARAPKAN

```
toti-cakery-chatbot/
├── README.md
├── MISSING_ENDPOINTS.md
├── docker-compose.yml
├── .env.example
├── chatbot-service/                 # service utama (FastAPI + LangChain)
│   ├── app/
│   │   ├── main.py
│   │   ├── core/config.py
│   │   ├── webhook/                 # terima event dari wwebjs-api
│   │   ├── whatsapp_client/         # wrapper pemanggil REST API wwebjs-api (kirim pesan, gambar)
│   │   ├── rag/                     # ingestion + retrieval ChromaDB
│   │   ├── llm/                     # setup Ollama, prompt template, parameter
│   │   ├── tools/                   # satu file per tool (get_menu.py, create_order.py, dst)
│   │   ├── conversation/            # state machine alur percakapan + session store
│   │   ├── backend_client/          # HTTP client ke FastAPI backend teammate
│   │   └── models/                  # SQLAlchemy models utk DB lokal chatbot (session, cart, conversation log)
│   ├── knowledge_base/
│   │   ├── faq/
│   │   │   ├── faq1.txt
│   │   │   ├── faq2.txt
│   │   │   └── ...                  # SATU FILE = SATU PERTANYAAN, lihat bagian 8
│   │   └── ingest.py                # script buat embed semua file ke ChromaDB
│   ├── tests/
│   └── requirements.txt
├── services/
│   └── payment-gateway/             # MOCK Midtrans, terisolasi, lihat bagian 12
│       ├── README.md                # dokumentasi kontrak API yang diharapkan utk versi real nanti
│       ├── mock_server.py
│       └── client_interface.py
└── whatsapp-gateway/
    └── docker-compose.override.yml  # konfigurasi wwebjs-api (image avoylenko/wwebjs-api), webhook ke chatbot-service
```

---

## 4. ENDPOINT BACKEND YANG SUDAH TERSEDIA (boleh dipakai langsung)

Ini hasil baca source code backend yang dikirim teammate saya. **PERINGATAN**: source punya bug double-prefix routing di `main.py` (setiap router di-include dengan prefix tambahan padahal sudah punya prefix sendiri). Path di bawah ini adalah path **logis/yang dimaksud**, bukan jaminan path final yang live — verifikasi dulu lewat `/docs` backend sebelum hardcode, atau tanya saya/backend engineer.

- `GET /products?only_active=true&kategori=...` — list produk/menu (untuk tool `get_menu`)
- `GET /products/{product_id}` — detail produk, termasuk `image_url`, `harga_jual`, `deskripsi`, `kategori`
- `GET /products/{product_id}/pricing` — breakdown HPP per bahan (kemungkinan tidak relevan untuk chatbot buyer)
- `GET /faq?only_active=true` — list FAQ (sumber awal knowledge base, lihat bagian 8 soal kenapa tetap pakai file lokal)
- `GET /faq/{faq_id}` — detail satu FAQ
- `GET /stock-items` — opsional, kalau mau cek stok bahan baku (bukan stok produk jadi)

Yang **TIDAK relevan** untuk chatbot buyer (jangan dipakai): `/auth/login` (login internal staff), `/expenses/*`, `/purchasing/*` (procurement ke supplier, bukan order customer), `/recipes/*`.

---

## 5. ENDPOINT BACKEND YANG **BELUM ADA** (jangan diasumsikan nyata)

Berdasarkan dokumen spesifikasi tim saya (skema tabel `customers`, `orders`, `order_items`, `invoices`, `payments`, `otp_tokens`, `chatbot_conversations`), tool-tool berikut **butuh endpoint backend yang sampai sekarang belum dikirim/belum dibangun**:

| Tool | Endpoint yang dibutuhkan (perkiraan, BUKAN final) | Status |
|---|---|---|
| `create_order_chatbot` | `POST /orders` (body: customer info, items[{product_id, qty}], metode_pengiriman) | ❌ Belum ada |
| `create_payment` | `POST /payments` atau lewat service Midtrans (lihat bagian 12) | ❌ Belum ada (memang sengaja di-mock) |
| `get_order_status` | `GET /orders?nomor_wa={phone}` atau `GET /customers/{phone}/orders/latest` | ❌ Belum ada |
| `escalate_to_admin` (human takeover) | sesuatu seperti `POST /customers/{phone}/takeover` + field status takeover di tabel `customers` | ❌ Belum ada, bahkan field penyimpanannya belum jelas ada di mana |
| `financial_report` / `business_analytics` (khusus Owner) | endpoint laporan keuangan & analitik | ❌ Belum ada (prioritas rendah, boleh dikerjakan paling akhir) |
| Registrasi/cek customer by nomor WA | `GET/POST /customers` | ❌ Belum ada |

Untuk tool-tool ini: implementasikan dengan **mock client** (lihat `backend_client/`, buat interface dulu lalu mock implementation), supaya alur percakapan & prompt engineering tetap bisa dikembangkan dan diuji end-to-end tanpa nunggu backend selesai. Setiap mock WAJIB dicatat di `MISSING_ENDPOINTS.md` dengan format:

```markdown
### create_order_chatbot
- Method/path diasumsikan: POST /orders
- Request body diasumsikan: { "customer": {...}, "items": [...], "metode_pengiriman": "pickup|delivery" }
- Response diasumsikan: { "order_id": int, "nomor_invoice": str, "total_harga": float }
- Alasan dibutuhkan: untuk mencatat pesanan dari chatbot ke database utama
```

---

## 6. ARSITEKTUR SISTEM

```
Customer (WhatsApp)
   │  pesan masuk/keluar
   ▼
wwebjs-api (Node.js, Docker, lib avoylenko)  ──webhook POST──▶  chatbot-service/app/webhook
   ▲                                                                   │
   │ REST: POST /client/sendMessage/:sessionId                        ▼
   └──────────────────────────────────────  conversation/state machine
                                                       │
                              ┌────────────────────────┼─────────────────────────┐
                              ▼                         ▼                         ▼
                          rag/ (ChromaDB +         tools/ (LangChain         backend_client/
                          qwen3-embedding)          tool calling)            (HTTP ke backend
                              │                         │                    teammate)
                              ▼                         ▼
                          llm/ (Ollama, qwen3:1.7b, tool calling enabled)
```

Alur teknis:
1. Pesan masuk dari customer diterima `wwebjs-api`, diteruskan via webhook (event `message`) ke `chatbot-service`.
2. `chatbot-service` ambil/​buat session state customer (by nomor WA) dari DB lokal SQLite.
3. State machine percakapan menentukan: ini pertanyaan umum (→ RAG) atau bagian dari alur transaksi (→ tool calling) atau command spesifik (cek status, batalkan, dll).
4. LangChain + Ollama (`qwen3:1.7b`) memutuskan apakah perlu retrieval (RAG) dan/atau tool call.
5. Hasil diformat jadi teks (dan/atau gambar lewat URL produk), dikirim balik via `POST /client/sendMessage/:sessionId` ke wwebjs-api.
6. Background task (lihat bagian 11) memantau status pembayaran & timeout pesanan secara berkala.

---

## 7. RAG / KNOWLEDGE BASE

- Sumber pengetahuan disimpan sebagai **file teks lokal, satu file = satu pertanyaan/topik**, di `knowledge_base/faq/faq1.txt`, `faq2.txt`, dst. Format tiap file bebas tapi konsisten, sarankan:
  ```
  Q: <pertanyaan>
  A: <jawaban>
  ```
- Buat `knowledge_base/ingest.py`: baca semua file di folder, embed pakai `qwen3-embedding:0.6b` (via Ollama), simpan ke ChromaDB persistent collection (mis. `chroma_db/` folder). Idempotent — re-run aman (upsert by file hash/id, bukan duplikat terus).
- Parameter embedding & retrieval (ikuti spesifikasi tim, taruh di config supaya gampang di-tune):
  - `chunk_size`: 512 token
  - `chunk_overlap`: 15% dari chunk_size (~77 token)
  - `top_k_retrieval`: 3 dokumen
- **Scope guard**: kalau skor similarity hasil retrieval di bawah threshold tertentu (taruh di config, mulai dari nilai wajar lalu boleh di-tune), chatbot **tidak boleh menjawab dari pengetahuan umum LLM** — harus balas dengan pesan standar bahwa chatbot ini cuma melayani seputar Toti Cakery. Ini penting, jangan dilewatkan.
- Selain FAQ, data produk untuk RAG **tidak perlu** di-embed manual ke ChromaDB — ambil real-time lewat tool `get_menu`/`get_product_detail` ke endpoint backend (data produk sering berubah harga/stok, jangan sampai stale di vector store).

---

## 8. LLM & TOOL CALLING SETUP

- Model: `qwen3:1.7b` via Ollama, pakai LangChain Ollama integration dengan tool/function calling enabled.
- Parameter inferensi (default, taruh di config):
  - `temperature`: 0.7
  - `top_p`: 0.8
  - `num_ctx`: 32768
- System prompt harus eksplisit menyatakan: identitas sebagai asisten Toti Cakery, hanya jawab seputar toko kue ini, gaya bahasa ramah & santai (Bahasa Indonesia, boleh respons Inggris kalau user pakai Inggris), dan instruksi kapan harus manggil tool vs jawab langsung dari RAG.
- Asumsikan Ollama sudah punya model `qwen3:1.7b` dan `qwen3-embedding:0.6b` ter-pull (atau versi fine-tuned-nya, kalau ada — itu di luar scope sesi ini, kamu cukup connect ke `OLLAMA_BASE_URL` yang dikasih lewat env). **Jangan bangun pipeline fine-tuning di sini.**

---

## 9. DAFTAR TOOLS (titik awal, boleh disesuaikan — lihat aturan #3)

| Tool | Fungsi | Status implementasi |
|---|---|---|
| `get_menu` | Ambil daftar menu aktif + harga + ketersediaan stok dari `GET /products` | ✅ Real, langsung ke backend |
| `get_product_detail` | Ambil detail 1 produk + `image_url` dari `GET /products/{id}` | ✅ Real |
| `compare_products` | Bandingkan 2+ produk (bisa diimplementasikan client-side dari hasil `get_product_detail`, tidak perlu endpoint baru) — **tidak mengirim gambar** | ✅ Real (logic lokal) |
| `create_order_chatbot` | Catat draft pesanan (item + qty) ke session lokal dulu; finalisasi ke backend saat checkout | ⚠️ Mock untuk bagian "simpan ke backend", session draft-nya boleh real (lokal) |
| `create_payment` | Generate QR + nomor VA via service Midtrans (lihat bagian 12) | ⚠️ Mock, panggil `services/payment-gateway` |
| `get_order_status` | Cek status pesanan terakhir customer by nomor WA | ⚠️ Mock |
| `cancel_order` | Batalkan pesanan yang masih pending | ⚠️ Mock |
| `escalate_to_admin` | Tandai sesi customer untuk human takeover, kirim notifikasi ke nomor admin | ⚠️ Partial — notifikasi WA ke admin bisa real (lewat wwebjs-api), tapi penyimpanan status takeover di backend itu mock |
| `financial_report`, `business_analytics` | Khusus Owner, lewat verifikasi nomor WA Owner | ⚠️ Mock, prioritas paling rendah, kerjakan terakhir kalau waktu cukup |

---

## 10. ALUR PERCAKAPAN (DETAIL, IKUTI URUTAN INI)

1. **Buka percakapan / tanya menu** → tool `get_menu`, balas daftar menu (nama + harga, ringkas, tanpa gambar massal).
2. **Tanya detail 1 menu** → tool `get_product_detail`, balas teks penjelasan + **kirim gambar** pakai `image_url` produk lewat wwebjs-api (`contentType: MessageMediaFromURL`).
3. **Tanya perbandingan 2+ menu** → tool `compare_products`, balas teks perbandingan saja, **TANPA gambar**.
4. **Pelanggan menyatakan ingin pesan** (bisa lebih dari satu jenis kue sekaligus, dengan jumlah/nominal masing-masing) → simpan sebagai draft order di session lokal, lalu **selalu balas dengan ringkasan pesanan** (daftar kue + jumlah per item) dan tanya konfirmasi: "sudah sesuai semua / mau nambah lagi?"
5. Pelanggan harus eksplisit konfirmasi (mis. ketik "sudah sesuai" / semacamnya) baru lanjut. Selama belum konfirmasi, pelanggan masih bisa ubah/nambah item.
6. Setelah konfirmasi → chatbot minta **identitas pelanggan**: nama, alamat, nomor HP (catatan: nomor WA pengirim sudah otomatis diketahui dari session WhatsApp, tapi tetap konfirmasi/biarkan diisi ulang sesuai keputusan kamu/desain UX — ⚠️ ASUMSI PERLU KONFIRMASI: apakah nomor HP perlu diketik ulang atau auto-fill dari nomor WA pengirim?).
7. **Validasi input identitas** — kalau ada yang kosong/format gak masuk akal (mis. nomor HP bukan angka), tanya ulang spesifik bagian yang salah saja, jangan minta isi ulang semua dari awal.
8. Setelah identitas valid → panggil `create_payment` → balas dengan **QR code + nomor Virtual Account** beserta nominal yang harus dibayar dan batas waktu pembayaran.
9. **Pembatalan**: pelanggan bisa minta batal kapan saja sebelum bayar → tool `cancel_order`. Juga, **background task** mengecek pesanan pending secara berkala (interval dari env `PAYMENT_CHECK_INTERVAL_SECONDS`); kalau lewat `PAYMENT_TIMEOUT_MINUTES` belum bayar, otomatis batalkan + kirim notifikasi WA ke pelanggan.
10. **Deteksi pembayaran otomatis**: background task yang sama (atau terpisah) polling status transaksi ke `services/payment-gateway` (mock); begitu status jadi "paid", langsung kirim notifikasi WA konfirmasi pembayaran ke pelanggan tanpa pelanggan perlu nanya duluan.
11. **Satu transaksi aktif per nomor WA**: kalau pelanggan masih punya order pending (belum bayar/belum selesai) dan mencoba bikin order baru, chatbot **menolak** dan mengarahkan untuk pesan lewat website saja.
12. **Cek status pesanan**: pelanggan bisa tanya progress kapan saja → tool `get_order_status`.
13. **Pesanan selesai/siap**: begitu status berubah jadi "ready" (dari sisi backend/admin — endpoint untuk update status ini ada di luar scope kamu, asumsikan ada webhook/polling lain yang memicu), chatbot **proaktif** kirim WA ke pelanggan bahwa pesanan siap diambil. Kalau metode pengiriman = delivery, ingatkan pelanggan bahwa pengiriman lewat GoSend/GrabExpress/sejenisnya dipesan sendiri oleh pelanggan ke alamat Toti Cakery, dan sertakan **nama penjual (Toti Cakery) serta alamat toko** dalam pesan supaya gampang dicopy ke aplikasi ojol.
14. **Custom cake / di luar kapabilitas bot** → tool `escalate_to_admin`, beri tahu pelanggan bahwa request diteruskan ke admin, dan kirim notifikasi ke nomor WA admin.

---

## 11. MIDTRANS — SUBFOLDER TERPISAH (MOCK)

Buat di `services/payment-gateway/`:
- `client_interface.py`: definisikan abstract interface (mis. `PaymentGatewayClient`) dengan method:
  - `create_transaction(order_id, amount, customer_name, customer_phone) -> {qr_url, va_number, bank, expiry_time}`
  - `get_transaction_status(order_id) -> {status: "pending"|"paid"|"expired"|"failed"}`
- `mock_server.py` (atau cukup mock class kalau gak perlu service HTTP terpisah, sesuaikan yang paling simpel): implementasi dummy yang generate data fake tapi realistis (nomor VA random, QR placeholder image/text, dsb), dengan delay/simulasi acak supaya bisa dites skenario "belum bayar" vs "sudah bayar" (boleh expose endpoint debug semacam `POST /debug/mark-paid/{order_id}` biar gampang testing manual).
- `README.md` di folder ini: dokumentasikan kontrak API yang DIHARAPKAN dari implementasi real nanti (request/response shape, auth, webhook format Midtrans), supaya teammate backend saya tinggal swap implementasi tanpa ubah chatbot-service.
- **Jangan** install package `midtransclient` atau sejenisnya, jangan ada API key/credential Midtrans asli di mana pun.

---

## 12. HUMAN TAKEOVER

- Saat tool `escalate_to_admin` dipanggil, chatbot:
  1. Set flag lokal `human_takeover_active = true` untuk nomor WA tsb di DB lokal session (dengan timestamp + `expires_at` = sekarang + 7 hari, sesuai spesifikasi tim saya).
  2. Kirim notifikasi ke nomor WA admin (ambil dari env `ADMIN_WA_NUMBER` untuk sekarang — endpoint untuk ambil nomor admin dinamis dari backend belum ada, catat di `MISSING_ENDPOINTS.md`).
  3. Selama flag aktif, chatbot **berhenti auto-reply** untuk nomor itu (pesan masuk tetap dicatat di log, tapi tidak diproses LLM) sampai expired atau di-reset manual.
- ⚠️ ASUMSI PERLU KONFIRMASI: di spesifikasi resmi tim saya, toggle takeover seharusnya dikontrol Admin lewat Admin Site (frontend web), bukan dari sisi chatbot. Karena endpoint itu belum ada, untuk sekarang implementasikan reset manual lewat cara paling simpel (misal endpoint internal `POST /internal/takeover/{phone}/deactivate` di chatbot-service sendiri yang bisa saya panggil manual lewat curl/Postman saat testing), bukan permanen.

---

## 13. WHATSAPP GATEWAY (wwebjs-api)

- Jalankan image `avoylenko/wwebjs-api` via docker-compose (referensi `docker-compose.yml` & `Dockerfile` aslinya akan saya lampirkan/sudah ada di reference, jangan ditulis ulang dari nol — tinggal dikonfigurasi).
- Set env `BASE_WEBHOOK_URL` mengarah ke endpoint webhook di `chatbot-service` (mis. `http://chatbot-service:8000/webhook/whatsapp`).
- Set `ENABLE_WEBHOOK=true`, dan kalau perlu filter event biar gak kebanjiran noise, pakai `DISABLED_CALLBACKS` untuk event yang gak relevan (kita cuma butuh event `message`, mungkin `message_ack` opsional untuk tracking delivery).
- Set `API_KEY` (x-api-key) untuk amankan komunikasi antar service di internal docker network.
- `chatbot-service` mengirim balasan lewat `POST {WWEBJS_BASE_URL}/client/sendMessage/{sessionId}` — gunakan `contentType: "string"` untuk teks biasa dan `contentType: "MessageMediaFromURL"` untuk kirim gambar produk.
- Session WhatsApp di-start manual sekali via `GET /session/start/{sessionId}` lalu scan QR (`GET /session/qr/{sessionId}/image`) — buat instruksi singkat soal ini di README, gak perlu otomatisasi rumit untuk capstone.

---

## 14. STATE / SESSION MANAGEMENT LOKAL

Karena backend belum punya tabel `customers`/`orders`, chatbot-service **punya database lokalnya sendiri** (SQLite) untuk:
- `sessions`: nomor WA, state percakapan saat ini, draft cart (JSON), timestamp.
- `chatbot_conversations` (lokal dulu, sesuai spesifikasi tabel di dokumen tim saya — kalau nanti backend sudah punya tabel yang sama, tinggal pindahkan): pesan masuk, respons AI, session_id, intent terdeteksi, timestamp.
- `pending_orders`: untuk tracking timeout pembayaran (lihat bagian 10 poin 9-10).

Ini **bukan** "menambah endpoint ke backend" — ini storage internal milik chatbot-service sendiri, jadi tidak melanggar aturan #1.

---

## 15. DELIVERABLES YANG DIHARAPKAN

1. Seluruh struktur folder di bagian 3, jalan lewat `docker-compose up`.
2. `README.md` — cara setup dari nol (env vars, cara start Ollama/pull model, cara link WhatsApp, cara run ingest knowledge base).
3. `MISSING_ENDPOINTS.md` — daftar lengkap semua endpoint backend yang di-mock, siap saya kirim ke backend engineer.
4. `.env.example` — semua env var yang dibutuhkan, terdokumentasi.
5. Minimal 3-5 file `knowledge_base/faq/faqN.txt` contoh (boleh isi dummy dulu, format sudah saya jelaskan di bagian 8) sebagai starter.
6. Test manual minimal: bisa kirim pesan WA "menu apa aja" dan dapat balasan benar dari `get_menu` real (bukan mock), karena ini satu-satunya tool yang endpointnya sudah pasti ada.

---

## 16. DI LUAR SCOPE (jangan dikerjakan)

- Frontend Buyer Site / Admin Site (React).
- Endpoint baru di repo backend utama teammate.
- Implementasi Midtrans yang sungguhan (SDK asli, API key asli).
- Fine-tuning model Qwen3 (anggap modelnya sudah/akan disediakan lewat Ollama).
- OTP WhatsApp untuk login Buyer Site (itu fitur web, bukan chatbot WhatsApp).
- Dashboard analitik visual — kalau tool `financial_report`/`business_analytics` dikerjakan, cukup return teks terformat ke chatbot, bukan bikin UI apa pun.

---

## 17. PERTANYAAN UNTUK SAYA SEBELUM/SELAMA LANJUT

Tolong konfirmasi ke saya (jangan asumsikan sendiri) untuk hal-hal berikut sebelum implementasi bagian terkait:
1. Nomor HP saat checkout: diketik ulang manual oleh pelanggan, atau auto-fill dari nomor WA pengirim pesan?
2. Skema pembayaran lewat chatbot WhatsApp: full payment saja, atau juga support DP 50% seperti di Buyer Site web?
3. Nomor WA admin untuk notifikasi (`ADMIN_WA_NUMBER`) — satu nomor fix, atau perlu dukung banyak admin?
4. Threshold skor similarity RAG untuk nentuin "di luar topik" — saya gak punya angka pasti, boleh kamu mulai dari nilai wajar dan saya akan tuning manual nanti, tapi tolong dijadikan satu variabel config yang jelas, bukan hardcoded di tengah kode.
