# Deploy ke VPS

Panduan dari VPS kosong sampai chatbot melayani WhatsApp. Seluruh stack jalan
sebagai container: **chatbot, backend, PostgreSQL, Ollama, wwebjs-api**.

Berbeda dari `README.md` (setup di laptop, backend dari Vercel), di sini
**backend ikut di-container** dan datanya di PostgreSQL milik VPS sendiri.

---

## 1. Spesifikasi VPS

| | Minimum | Disarankan | Kenapa |
|---|---|---|---|
| RAM | 8 GB | 12 GB | angka terukur di bawah |
| vCPU | 4 | 4–8 | inferensi 100% CPU; makin sedikit core makin lama balasannya |
| Disk | 20 GB | 40 GB | image Docker ± 12 GB + model ± 2 GB + Postgres |
| Tipe | **KVM** | KVM | container/OpenVZ sering tidak bisa jalankan Docker |
| OS | Ubuntu 22.04/24.04 LTS | — | apa saja yang bisa Docker |

**Pemakaian RAM model (diukur, bukan perkiraan):**

| Yang residen | RAM |
|---|---|
| `toti-qwen-1.7b-v4` dengan `LLM_NUM_CTX=32768` (default) | **5,2 GB** |
| `toti-qwen-1.7b-v4` dengan `LLM_NUM_CTX=8192` | **2,4 GB** |
| `qwen3-embedding:0.6b` | **1,4 GB** |
| backend + postgres + chatbot + Chromium (wwebjs) | ± 1,5 GB |

`OLLAMA_KEEP_ALIVE=-1` membuat kedua model tetap di RAM (biar tidak ada reload
±15 detik tiap pelanggan baru). Jadi total ± 8 GB dengan setelan default.

**VPS 4–6 GB?** Turunkan `LLM_NUM_CTX=8192` di `.env` — hemat 2,8 GB. Konteks
8k masih cukup untuk percakapan pemesanan biasa; kalau percakapan sangat panjang
mulai terpotong, naikkan lagi. Sediakan swap (langkah 2) apa pun ukuran RAM-nya.

---

## 2. Siapkan VPS

```bash
# login sebagai root, lalu buat user biasa (jangan jalankan stack sebagai root)
adduser toti && usermod -aG sudo toti
su - toti

# Docker + Compose v2 (skrip resmi Docker)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
docker compose version        # harus v2.x

# swap 4 GB — pengaman kalau RAM mepet saat model dimuat
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**Firewall**: selama pembayaran belum dinyalakan, stack ini **tidak butuh port
masuk satu pun** selain SSH. Semua service dipublish ke `127.0.0.1` saja dan
saling bicara lewat network internal Docker; ke luar cuma perlu koneksi keluar
(WhatsApp, Midtrans, HuggingFace).

⚠️ **Satu pengecualian begitu Midtrans dipakai:** status pembayaran HANYA berubah
lewat webhook `POST /payments/notify` yang ditembak server Midtrans — backend
tidak pernah menanyakan status ke Midtrans sendiri. Kalau endpoint itu tidak bisa
dijangkau dari internet, pembayaran pelanggan **selamanya "Pending"**: invoice
tak pernah lunas, pesanan tak pernah diproses. Jadi kalau toko sudah menerima
pembayaran, langkah 10 (domain + Caddy) **wajib**, bukan opsional.

```bash
sudo ufw allow OpenSSH && sudo ufw enable
```

Port 80/443 baru dibuka nanti kalau backend mau diekspos (langkah 10).

---

## 3. Ambil kode

```bash
git clone git@github.com:kevinilhamramadhan/chatbot-capstone.git ~/chatbot
cd ~/chatbot
```

Yang **tidak** ikut di git dan harus disiapkan sendiri: `.env` (langkah 4) dan
bobot model fine-tune (langkah 5).

Source backend tidak perlu di-clone manual — `backend/Dockerfile` mengambilnya
sendiri dari `github.com/Nicholl2/Backend-Cakery` pada commit yang dipin
(`BACKEND_REF`). Repo teammate tidak pernah kita ubah.

---

## 4. Isi `.env`

```bash
cp .env.example .env
openssl rand -hex 24    # jalankan beberapa kali untuk nilai di bawah
```

Wajib diisi (stack menolak start kalau kosong):

| Var | Isi |
|---|---|
| `WEBHOOK_TOKEN` | `openssl rand -hex 24` — rahasia di path webhook WhatsApp |
| `INTERNAL_API_KEY` | `openssl rand -hex 24` — dipakai backend saat memanggil chatbot |
| `WWEBJS_API_KEY` | `openssl rand -hex 24` — kunci gateway WhatsApp |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` — **pakai hex**, karakter `@ : /` merusak URL koneksi |
| `BACKEND_SECRET_KEY` | `openssl rand -hex 32` — kunci JWT login admin backend |
| `BACKEND_SERVICE_API_KEY` | bebas, acak — dipakai kedua sisi sekaligus, jadi tidak mungkin mismatch |

Perlu disesuaikan: `ADMIN_WA_NUMBER`, `OWNER_WA_NUMBERS` (format `628…`),
`STORE_NAME`, `STORE_ADDRESS`. Opsional: `MIDTRANS_SERVER_KEY` (kosongkan kalau
belum uji pembayaran — sisa fitur tetap jalan; kunci **sandbox** dan
**production** sama-sama berbentuk `Mid-server-…`, yang membedakan environment
adalah `MIDTRANS_IS_PRODUCTION`), `HF_TOKEN` (langkah 5).

> `BACKEND_BASE_URL`, `OLLAMA_BASE_URL`, `WWEBJS_BASE_URL` di `.env` **ditimpa**
> oleh `docker-compose.yml` dengan nama container. Tidak perlu diubah.

---

## 5. Sediakan model fine-tune

`toti-qwen-1.7b-v4` bukan model publik — tidak bisa `ollama pull`. Pilih satu:

**A. Unduh dari HuggingFace (disarankan).** Repo `LasagnaS/toti-qwen-gguf`
bersifat private dan itu tidak masalah: kuota private akun free 100 GB, repo ini
baru 11 GB, jadi gratis.

1. Buat **fine-grained token** di <https://huggingface.co/settings/tokens> —
   akses **Read**, dibatasi ke repo `LasagnaS/toti-qwen-gguf` saja (jangan token Write).
2. Isi `HF_TOKEN=hf_...` di `.env`. Bootstrap akan mengunduh sendiri (1,1 GB).

**B. Salin manual dari mesin dev** (kalau tidak mau menaruh token di VPS):

```bash
# dijalankan DI LAPTOP
scp finetune/toti-qwen-1.7b.Q4_K_M.gguf.v4 toti@<ip-vps>:~/chatbot/finetune/
```

Jangan pakai `ollama pull hf.co/...` walaupun repo-nya dibuat publik: cara itu
memakai chat template bawaan GGUF, bukan `finetune/Modelfile.qwen3-1.7b-v4`
yang berisi penanganan `/think` + parameter sampling hasil eval v4.

---

## 6. Jalankan bootstrap

```bash
./scripts/bootstrap.sh
```

Idempoten — aman diulang. Yang dikerjakan: buat file SQLite chatbot, bangun
model Ollama dari GGUF, tarik model embedding, build + start semua container,
lalu ingest FAQ ke ChromaDB. Build backend meng-clone repo, jadi bagian ini
butuh beberapa menit pada percobaan pertama.

Verifikasi:

```bash
docker compose ps                              # 5 service "running"/"healthy"
curl localhost:8000/health                     # chatbot  -> {"status":"ok",...}
curl -s localhost:8001/openapi.json | head -c 80   # backend -> JSON OpenAPI
docker compose exec ollama ollama list         # ada toti-qwen-1.7b-v4 + embedding
```

---

## 7. Isi data awal (DB backend masih kosong)

Skema tabel dibuat otomatis saat backend start, **tapi tidak ada seed sama
sekali** — belum ada produk, FAQ, maupun user admin. Dan backend tidak punya
endpoint pendaftaran user internal, jadi user pertama harus dimasukkan lewat SQL.

**a. Role + user Owner pertama**

```bash
# 1) hash password (ganti PASSWORD_KAMU)
docker compose exec -T backend python -c \
  "import bcrypt; print(bcrypt.hashpw(b'PASSWORD_KAMU', bcrypt.gensalt()).decode())"

# 2) masukkan role + owner (tempel hash dari langkah 1, dan nomor WA admin)
docker compose exec -T postgres psql -U toti -d toti <<'SQL'
INSERT INTO roles (id, nama_role, level)
VALUES (1,'Owner',1), (2,'Admin',2), (3,'Staff',3)
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (username, password_hash, role_id, is_active, nomor_wa_admin, handles_takeover)
VALUES ('owner', '<TEMPEL_HASH_DI_SINI>', 1, true, '628xxxxxxxxxx', true);
SQL

# 3) uji login -> dapat access_token
curl -X POST localhost:8001/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"owner","password":"PASSWORD_KAMU"}'
```

**b. Produk** (`POST /products/` tidak butuh token):

```bash
curl -X POST localhost:8001/products/ -H 'Content-Type: application/json' -d '{
  "nama_produk": "Brownies Panggang",
  "deskripsi": "Brownies cokelat panggang, loyang 20x20",
  "kategori": "Brownies",
  "harga_jual": 85000,
  "minimum_order": 1,
  "is_active": true
}'
curl -s "localhost:8001/products/?only_active=true"    # cek masuk
```

**c. FAQ backend** (butuh token Owner/Admin dari langkah a):

```bash
TOKEN=<access_token>
curl -X POST localhost:8001/faq -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"pertanyaan":"Berapa lama pengerjaan pesanan?","jawaban":"H-2 sebelum tanggal ambil."}'
```

> FAQ yang dipakai RAG chatbot **bukan** ini, melainkan file
> `chatbot-service/knowledge_base/faq/*.txt`. Setiap kali file itu diubah:
> `docker compose exec chatbot-service python knowledge_base/ingest.py`.

**Punya data di Neon dan mau dipindah?** Sekali jalan:

```bash
pg_dump "<URL_NEON>" -Fc -f toti.dump                 # di laptop
scp toti.dump toti@<ip-vps>:~/                        # kirim
docker compose exec -T postgres pg_restore -U toti -d toti --clean --if-exists < ~/toti.dump
```

---

## 8. Hubungkan WhatsApp (sekali, manual)

Port 3000 sengaja tidak terbuka ke internet, jadi QR diambil lewat SSH tunnel:

```bash
# DI LAPTOP
ssh -L 3000:localhost:3000 toti@<ip-vps>
```

Selama tunnel hidup, port 3000 VPS terlihat sebagai `localhost:3000` di laptop.
Jalankan semuanya **dari laptop** (isi `KEY` dengan `WWEBJS_API_KEY` di `.env` VPS):

```bash
KEY=<WWEBJS_API_KEY>
curl -H "x-api-key: $KEY" http://localhost:3000/session/start/toti
# -> {"success":true,"message":"Session initiated successfully"}

# tunggu ±10 detik sampai state UNPAIRED, lalu ambil QR-nya
curl -H "x-api-key: $KEY" http://localhost:3000/session/status/toti     # -> "UNPAIRED"
curl -H "x-api-key: $KEY" http://localhost:3000/session/qr/toti/image -o qr.png
xdg-open qr.png
```

> Kuncinya **wajib lewat header** `x-api-key`. Versi wwebjs-api sekarang menolak
> `?x-api-key=...` di query string dengan `403 Invalid API key` — jadi membuka URL
> QR langsung di browser tidak jalan; ambil PNG-nya pakai `curl` seperti di atas.

Scan dari WhatsApp → Perangkat tertaut → Tautkan perangkat. Cek hasilnya:

```bash
curl -H "x-api-key: $KEY" http://localhost:3000/session/status/toti
# -> {"success":true,"state":"CONNECTED","message":"session_connected"}
```

Sesi tersimpan di `whatsapp-gateway/sessions/` — selama folder itu utuh, tidak
perlu scan ulang meski container restart.

---

## 9. Uji end-to-end

Kirim WhatsApp dari nomor lain ke nomor toko: "menu apa aja?" — jawaban harus
menyebut produk dari langkah 7b. Sambil menunggu:

```bash
docker compose logs -f chatbot-service
```

Balasan pertama lebih lambat (model baru dimuat). Kalau bot menjawab tapi
menunya kosong, backend/DB-nya yang belum terisi, bukan chatbot-nya.

---

## 10. Buka backend ke internet (wajib kalau pembayaran dipakai)

Dua alasan: (a) webhook Midtrans `/payments/notify` harus bisa masuk, (b) nanti
frontend React memanggil backend.

> ⚠️ **Baca dulu:** `POST /products/` di backend saat ini **tanpa autentikasi**,
> dan CORS-nya `*`. Begitu domainnya publik, siapa pun yang tahu URL-nya bisa
> membuat produk. Itu kode teammate (kita tidak mengubah backend) — minta dia
> menambahkan proteksi role di endpoint itu sebelum benar-benar go-live, atau
> batasi aksesnya di Caddy.

1. Beli domain, arahkan **A record** ke IP VPS (mis. `api.totica.com`).
2. `DOMAIN=api.totica.com` di `.env`.
3. Buka port: `sudo ufw allow 80 && sudo ufw allow 443`
4. Jalankan dengan overlay Caddy:

```bash
docker compose -f docker-compose.yml -f docker-compose.caddy.yml up -d
curl https://api.totica.com/openapi.json | head -c 80
```

Caddy mengurus sertifikat Let's Encrypt otomatis. Yang diekspos **hanya
backend** — chatbot dan gateway WhatsApp tetap tertutup. Simpan volume
`caddy_data` (berisi sertifikat; Let's Encrypt punya rate limit).

5. Daftarkan URL webhook di dashboard Midtrans → **Settings → Configuration →
   Payment Notification URL**:

   ```
   https://api.domainmu.com/payments/notify
   ```

   Uji sekali: bikin satu pesanan kecil, bayar lewat
   <https://simulator.sandbox.midtrans.com>, lalu pastikan statusnya berubah:

   ```bash
   curl -s -H "X-Service-Key: $BACKEND_SERVICE_API_KEY" \
     localhost:8001/payments/<order_id>/status
   # invoice_status harus "paid", payment_status "Success"
   ```

   Kalau masih `Pending`, webhook-nya tidak sampai — cek log Caddy dan riwayat
   notifikasi di dashboard Midtrans (di sana ada tombol kirim ulang).

---

## 11. Operasional

```bash
docker compose ps                       # status
docker compose logs -f chatbot-service  # log satu service
docker compose restart chatbot-service  # restart
docker compose down                     # matikan (data volume tetap aman)
docker compose up -d                    # nyalakan lagi
```

**Update chatbot:**
```bash
git pull && docker compose up -d --build chatbot-service
```

**Update backend** ke commit terbaru teammate:
```bash
git ls-remote https://github.com/Nicholl2/Backend-Cakery HEAD   # ambil SHA
# ganti BACKEND_REF di .env
docker compose up -d --build backend
```

**Ganti model** (v5 nanti): taruh GGUF baru di `finetune/`, sesuaikan
`Modelfile`-nya, ubah `LLM_MODEL` di `.env`, lalu `./scripts/bootstrap.sh`.

**Backup** — yang wajib rutin:

| Apa | Perintah |
|---|---|
| DB backend (pesanan, produk, user) | `docker compose exec -T postgres pg_dump -U toti -Fc toti > backup-$(date +%F).dump` |
| Sesi WhatsApp (hilang = scan QR ulang) | `tar czf sessions-$(date +%F).tgz whatsapp-gateway/sessions/` |
| DB chatbot (percakapan, keranjang) | `cp chatbot-service/toti_chatbot.db backup/` |
| Foto produk | `docker run --rm -v chatbot_backend_static:/s -v $PWD:/b alpine tar czf /b/static.tgz /s` |

Simpan salinannya **di luar VPS**.

---

## 12. Kalau bermasalah

| Gejala | Penyebab & solusi |
|---|---|
| `toti_chatbot.db` jadi direktori, chatbot gagal start | `docker compose up` dijalankan sebelum bootstrap. `sudo rm -rf chatbot-service/toti_chatbot.db` lalu `./scripts/bootstrap.sh` |
| Chatbot: `model "toti-qwen-1.7b-v4" not found` | Model belum dibuat. `docker compose exec ollama ollama list`, lalu ulangi langkah 5–6 |
| Bot diam saja saat di-WhatsApp | Sesi terputus: cek `session/status/toti`; atau `WEBHOOK_TOKEN` di `.env` berubah setelah gateway start — `docker compose up -d wwebjs-api` |
| Sesi lama nyangkut: `session_not_found` + `ProtocolError: Execution context was destroyed` berulang di log | Data sesi di `whatsapp-gateway/sessions/session-<id>` sudah basi/ter-logout. Matikan gateway, hapus foldernya, mulai lagi: `docker compose stop wwebjs-api && sudo rm -rf whatsapp-gateway/sessions/session-toti && docker compose start wwebjs-api`, lalu ulangi langkah 8 |
| QR balas `403 Invalid API key` | Kunci dikirim di query string. Harus header: `curl -H "x-api-key: $KEY" .../session/qr/toti/image -o qr.png` |
| `toti-wwebjs` restart terus | Chromium kehabisan `/dev/shm`. Sudah diatasi lewat `shm_size: 512m`; kalau masih, cek RAM sisa (`free -h`) |
| Backend `unhealthy` | Biasanya Postgres/URL. `docker compose logs backend`; pastikan `POSTGRES_PASSWORD` hex tanpa `@ : /` |
| Balasan lambat sekali | Normal untuk CPU-only; kurangi `LLM_NUM_CTX`, atau tambah vCPU |
| Container mati saat menjawab (OOM) | RAM kurang. `LLM_NUM_CTX=8192`, pastikan swap aktif |
| Menu kosong padahal bot menjawab | Katalog backend kosong — langkah 7b |
| Jawaban FAQ selalu "di luar topik" | Ingest belum jalan: `docker compose exec chatbot-service python knowledge_base/ingest.py` |
