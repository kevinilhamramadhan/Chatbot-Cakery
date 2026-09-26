# Toti Cakery — Chatbot WhatsApp

Chatbot WhatsApp untuk toko kue Toti Cakery: menjawab FAQ (RAG), menampilkan
menu, menerima pesanan sampai tagihan Midtrans terbit, mengabari status
pembayaran dan "pesanan siap", meneruskan pelanggan ke admin, dan memberi
laporan ke Owner. Dibangun dengan FastAPI, LangChain, Ollama (model fine-tune
`toti-qwen-1.7b-v9`), ChromaDB, dan gateway `avoylenko/wwebjs-api`.

Repo ini hanya berisi chatbot dan fine-tuning modelnya. Backend (FastAPI +
PostgreSQL) dan frontend adalah repo milik anggota tim lain; deploy seluruh
stack ada di repo **Deploy-Toti-Cakery**.

## Isi

- [Arsitektur](#arsitektur)
- [Struktur repo](#struktur-repo)
- [Menjalankan di laptop](#menjalankan-di-laptop)
- [Tes dan QA](#tes-dan-qa)
- [CI/CD](#cicd)
- [Kontrak dengan backend](#kontrak-dengan-backend)
- [Fine-tuning model](#fine-tuning-model)
- [Keputusan desain yang perlu diketahui](#keputusan-desain-yang-perlu-diketahui)

## Arsitektur

```
Pelanggan (WhatsApp)
   ▼
wwebjs-api ──webhook──▶ chatbot-service  /webhook/whatsapp/<WEBHOOK_TOKEN>
                            │  orchestrator (state machine + langkah deterministik)
             ┌──────────────┼──────────────────┐
             ▼              ▼                  ▼
       rag/ (ChromaDB   llm/ (Ollama,     backend_client/ (HTTP ke backend:
       + embedding)     tool calling)     produk, pesanan, pembayaran,
                            │             takeover, laporan, verifikasi WA)
                            ▼
                        tools/ (LangChain)
```

- **Langkah deterministik dulu, model kemudian.** Konfirmasi keranjang, data
  pelanggan, metode kirim, jenis dan kanal bayar, konfirmasi batal, tawaran
  sambung admin, rekomendasi, dan "nggak jadi" ditangani kode. Sisanya
  (pertanyaan bebas) dijawab model dengan tool.
- **Keluaran tool dikirim apa adanya**, tanpa diolah model lagi — harga dan
  ringkasan pesanan selalu dari data asli. Harga yang diketik model sendiri
  dibuang.
- **FAQ** diambil dari backend (dikelola lewat Admin Site) dan disegarkan tiap
  `FAQ_REFRESH_SECONDS`; `knowledge_base/faq/*.txt` hanya cadangan.
- **Data milik chatbot** ada di SQLite sendiri: sesi, log percakapan, pelacak
  pembayaran.
- **Tool pelanggan:** `get_menu`, `get_product_detail`, `compare_products`,
  `add_to_cart`, `check_cart`, `get_order_status`, `check_payment_status`,
  `resend_payment_method`, `cancel_order`, `escalate_to_admin`, `send_apology`.
  Owner mendapat tambahan `financial_report` dan `business_analytics`; definisi
  keduanya tidak pernah dikirim ke model untuk pelanggan biasa.
- **Dua bahasa.** Bahasa sesi ditentukan kode (butuh dua kata pencocok) dan
  dikunci; model diberi arahan bahasa di blok system.

## Struktur repo

```
chatbot-service/
  app/
    conversation/   orchestrator, state, checkout, bahasa, eskalasi, verifikasi
    llm/            agent (RAG + tool calling), prompt, klien Ollama
    rag/            ChromaDB, embedding, sumber FAQ backend
    tools/          tool LangChain
    backend_client/ HTTP ke backend
    webhook/        endpoint WhatsApp + /webhook/internal/*
  knowledge_base/   FAQ cadangan + ingest.py
  scripts/          chat_cli, qa_runner (+ skenario), smoke_live
  tests/            pytest (hermetis, tanpa layanan luar)
finetune/           generator dataset, audit, eval, notebook Colab, Modelfile v7 & v9
```

## Menjalankan di laptop

Butuh Python 3.11+, Ollama berisi model `toti-qwen-1.7b-v9` dan
`qwen3-embedding:0.6b`, serta backend yang bisa dijangkau (lokal atau server).

```bash
cp .env.example chatbot-service/.env     # isi WEBHOOK_TOKEN, INTERNAL_API_KEY,
                                         # WWEBJS_API_KEY, BACKEND_SERVICE_API_KEY
cd chatbot-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python knowledge_base/ingest.py          # isi ChromaDB dari FAQ
python -m scripts.chat_cli               # ngobrol lewat terminal, tanpa WhatsApp
uvicorn app.main:app --reload --port 8000   # atau jalankan service-nya
```

Service menolak start tanpa `WEBHOOK_TOKEN`, `INTERNAL_API_KEY`, dan
`WWEBJS_API_KEY`. `BACKEND_SERVICE_API_KEY` harus sama persis dengan
`SERVICE_API_KEY` backend; kalau beda, semua tool transaksi dijawab 401.

Model fine-tune dibuat dari GGUF di Hugging Face:

```bash
ollama pull hf.co/LasagnaS/toti-qwen-1.7b-v9-gguf:Q4_K_M
sed 's#^FROM .*#FROM hf.co/LasagnaS/toti-qwen-1.7b-v9-gguf:Q4_K_M#' \
    finetune/Modelfile.qwen3-1.7b-v9 > /tmp/Modelfile
ollama create toti-qwen-1.7b-v9 -f /tmp/Modelfile
ollama pull qwen3-embedding:0.6b
```

## Tes dan QA

```bash
cd chatbot-service && pytest -q
```

Suite-nya hermetis: semua env dipaksa, SQLite sementara, backend/Ollama/WhatsApp
di-stub. Setiap bug yang ditemukan di QA langsung diberi tes regresi
(`tests/test_qa_regressions.py`).

QA percakapan terhadap model dan backend sungguhan (WhatsApp di-stub):

```bash
python -m scripts.qa_runner scripts/qa_demo.json /tmp/hasil.json
```

Di server, jalankan di dalam container `chatbot-service` dengan nomor uji
`62999…`, lalu bersihkan sesinya dengan `reset-percakapan.sh` dari repo
Deploy-Toti-Cakery (pesanan uji yang masih pending ikut dibatalkan).

## CI/CD

```
push ke main ─▶ CI: pytest ─▶ build image ─▶ smoke test ─▶ push GHCR
                (:latest, :main, :sha-<commit>)
                                   │
server ◀── WUD memeriksa digest :latest tiap 5 menit, lalu merekreasi container
```

- CI (`.github/workflows/ci.yaml`) jalan untuk push dan pull request; image
  hanya di-push dari `main`. Tag `v*` menambah tag semver.
- Di server, WUD (What's Up Docker, di repo Deploy-Toti-Cakery) memasang
  `:latest` yang baru secara otomatis — backend dan frontend juga. Dari push
  sampai terpasang ±8 menit.
- Rollback: set `CHATBOT_IMAGE=ghcr.io/kevinilhamramadhan/chatbot-cakery:sha-<commit>`
  di `.env` server lalu `docker compose up -d chatbot-service`. Selama image
  tidak menunjuk `:latest`, WUD tidak mengubahnya.

## Kontrak dengan backend

Semua panggilan chatbot → backend membawa `X-Service-Key`. Semua panggilan
backend → chatbot ke `/webhook/internal/*` membawa `X-Internal-Key`
(`INTERNAL_API_KEY`); auth yang gagal sengaja dijawab 404.

**Yang dipakai chatbot dari backend** (base URL termasuk `/api`):

| Method | Path | Untuk |
|---|---|---|
| GET | `/products/`, `/products/{id}` | menu, detail, foto (`image_url` berupa path relatif) |
| POST | `/customers` | simpan/ambil pelanggan (`nomor_wa`, `nama`, `alamat`) |
| POST | `/orders` | buat pesanan |
| GET | `/orders/latest?nomor_wa=` | status pesanan terakhir |
| POST | `/orders/{id}/cancel` | batalkan (stok kembali) |
| POST | `/payments`, GET `/payments/{order_id}/status` | charge Midtrans + polling |
| GET/POST | `/customers/{nomor}/takeover` | status takeover admin |
| GET | `/admin/takeover-handlers` | nomor admin penerima takeover |
| GET | `/users/owner-numbers` | nomor Owner |
| GET | `/reports/financial-summary` | laporan Owner |
| POST | `/auth/verify/wa/confirm` | verifikasi nomor untuk pendaftaran Buyer Site |

Chatbot tidak pernah memanggil Midtrans langsung.

**Yang dipanggil backend ke chatbot** (`CHATBOT_URL` = base URL tanpa path):

| Path | Kapan |
|---|---|
| `POST /webhook/internal/orders/{id}/ready` | admin menandai pesanan siap |
| `POST /webhook/internal/orders/{id}/paid` | pembayaran lunas |
| `POST /webhook/internal/orders/{id}/refunded` | dana refund sudah ditransfer |
| `POST /webhook/internal/takeover/{nomor}/deactivate` | admin selesai menangani |
| `GET /webhook/internal/wa/status`, `/wa/qr`, `POST /wa/ganti-nomor` | halaman WhatsApp Chatbot di Admin Site |

Semua endpoint kabar bersifat idempoten; `{"status": "not_found"}` berarti
pesanannya bukan dari WhatsApp, bukan error.

**Nomor telepon kanonik** di kedua sisi: buang selain angka, awalan `0` → `62`,
`620` → `62`, harus diawali `62`, panjang 10–15 digit.

## Fine-tuning model

Model: Qwen3-1.7B + LoRA (Unsloth, Colab), diekspor ke GGUF Q4_K_M.

| | |
|---|---|
| Dataset | `LasagnaS/toti-cakery-toolcall` — 1600 train / 162 val / 166 test, ID ±78% / EN ±22% |
| Model | `LasagnaS/toti-qwen-1.7b-v9-gguf` (produksi), v7 disimpan untuk rollback |
| Generator | `finetune/generate_dataset.py` (seed 42, tanpa LLM; templat + slot dari katalog asli) |
| Audit | `finetune/audit_dataset.py` — 41 pemeriksaan terhadap kode runtime |
| Eval | `finetune/eval_tool_calling.py`, `scenario_suite.py`; notebook membandingkan base vs fine-tune pada kondisi produksi |
| Notebook | `finetune/finetune_toti_qwen3.ipynb` (LoRA r/alpha 16, lr 2e-4, 2 epoch, batch efektif 8) |

Baris dataset dibangun dari kode runtime yang sama (`SYSTEM_PROMPT`,
`TOOL_REMINDER`, skema tool, `_history_view`, `pertanyaan_dengan_konteks`),
jadi prompt latih identik dengan prompt produksi. Hanya giliran asisten
terakhir yang dilatih.

Riwayat singkat:

- **v7** — konteks FAQ di posisi produksi (66% baris), FAQ sebagai kemampuan
  membaca (fakta berbeda tiap baris), semua tool punya contoh.
- **v8** — arahan bahasa sesi di blok system, giliran pengisi ("ok", "iya")
  dilatih. Gagal: sesi Indonesia bercampur Inggris 8/25 (v7: 1/25).
- **v9** — penambahan v8 dikembalikan; tipe baru N14 (pasangan kontras yang
  hanya berbeda bahasa). Hasil A/B di VM: campur Inggris 0/25, kata karangan
  0/25, tool palsu 0/12. **Dipakai di produksi.**

## Keputusan desain yang perlu diketahui

- **Prefix KV-cache menentukan latensi.** Blok system + definisi tool ±2.400
  token; kalau tidak ada di cache Ollama, CPU butuh ±55 detik untuk memprosesnya
  ulang. Karena itu blok system dijaga konstan per bentuk (bahasa × peran),
  konteks FAQ menumpang di pesan pelanggan, dan pemanasan saat start mengirim
  keempat bentuk prompt yang persis sama dengan giliran sungguhan
  (`llm.agent.pesan_pembuka`). Hasilnya: balasan 5–22 detik, dan setelah VM
  menyala pemanasan butuh ±3,5 menit.
- **Riwayat dibersihkan sebelum masuk model.** Menu, detail produk, takeover,
  dan permintaan maaf diganti penanda singkat; pasangan tanya-jawab yang
  dijawab "belum punya info" dibuang. Model 1,7B menyalin contoh terkuat di
  riwayat.
- **Takeover hanya setelah pelanggan setuju.** Bot menawarkan dulu; takeover
  membungkam bot sampai `TAKEOVER_EXPIRY_DAYS`, jadi perlu izin eksplisit.
- **Keluhan dijawab kalimat tetap** (`send_apology`) — model kecil pernah
  menjawab "kuenya basi" dengan "senang kalau suka".
- **Nomor `@lid` wajib diterjemahkan** ke nomor telepon sebelum dipakai; angka
  LID tidak boleh sampai ke backend.
- **Data pribadi**: isi pesan tidak dicatat di log kecuali
  `LOG_MESSAGE_BODIES=true`, nomor disamarkan, transkrip dihapus setelah
  `DATA_RETENTION_DAYS`.
