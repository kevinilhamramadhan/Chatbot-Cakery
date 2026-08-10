# Konfigurasi Backend ⇄ Chatbot Service

Panduan singkat buat **Nicholas (Backend-Cakery)**: apa saja yang perlu di-set di sisi
backend supaya nyambung dengan Chatbot Service (WhatsApp bot Toti Cakery).

Ringkasnya cuma **dua hal**:

1. `CHATBOT_URL` — alamat Chatbot Service, dipakai backend buat push "pesanan siap".
2. `SERVICE_API_KEY` — harus **sama persis** dengan yang dipasang di `.env` chatbot.

Selebihnya sudah jalan otomatis.

---

## 1. `CHATBOT_URL` — isinya apa?

**Base URL saja, tanpa path dan tanpa trailing slash.** Path-nya sudah kamu susun
sendiri di kode push (`{CHATBOT_URL}/webhook/internal/orders/{id}/ready`).

| Backend-mu jalan di mana | Isi `CHATBOT_URL` |
|---|---|
| Lewat `docker compose up` di repo gabungan (service `backend`) | `http://chatbot-service:8000` — **sudah otomatis di-set** di `docker-compose.yml`, tidak perlu ditambah ke `.env` |
| `uvicorn` langsung di mesin yang sama dengan chatbot | `http://localhost:8000` |
| Server/VPS lain | ❌ belum bisa — lihat catatan di bawah |

> **Catatan penting:** Chatbot Service saat ini **belum di-deploy** ke server mana pun.
> Port 8000-nya sengaja di-bind ke `127.0.0.1` saja (bukan `0.0.0.0`) karena endpoint
> `/webhook/*` belum punya autentikasi, jadi tidak boleh terbuka ke LAN/internet.
> Artinya: backend yang jalan di mesin/server **lain** akan kena `Connection refused`.
> Kalau chatbot sudah dideploy, Kevin akan kasih URL publiknya — cukup ganti nilai
> `CHATBOT_URL`, tidak ada perubahan kode.

`.env` backend:

```env
CHATBOT_URL=http://localhost:8000
```

---

## 2. Endpoint chatbot yang dipanggil backend

Cuma **satu**, yaitu push C4 "pesanan siap":

```
POST {CHATBOT_URL}/webhook/internal/orders/{order_id}/ready
```

| Hal | Keterangan |
|---|---|
| Kapan dipanggil | Saat admin menandai order jadi siap (status → *siap/ready*) |
| `order_id` | ID order dari database backend (`orders.id`) — sama persis dengan yang dikembalikan `POST /orders` |
| Body | **Tidak perlu** (boleh kosong). Chatbot mengambil semua data dari `order_id` |
| Header auth | **Tidak perlu**. `X-Service-Key` itu untuk arah sebaliknya (chatbot → backend) |
| Response 200 | `{"status": "ok", "order_id": 12}` → pesan WA sudah dikirim ke pelanggan |
| Response 200 | `{"status": "not_found", "order_id": 12}` → order-nya tidak ada di sisi chatbot (mis. order dibuat lewat website, bukan lewat WA). **Bukan error**, tidak usah di-retry terus |
| Idempoten | Ya. Kalau dipanggil berulang untuk order yang sama, pelanggan hanya dikirimi pesan sekali |

Panggilannya sebaiknya *fire-and-forget* (jangan bikin update status di backend gagal
hanya karena chatbot lagi mati) — cukup `try/except` + log.

Cek cepat kalau mau tes:

```bash
# health check chatbot
curl http://localhost:8000/health
# -> {"status":"ok","service":"Toti Cakery Chatbot Service"}

# simulasi push "pesanan siap" untuk order 12
curl -X POST http://localhost:8000/webhook/internal/orders/12/ready
# -> {"status":"ok","order_id":12}  (atau "not_found" kalau order-nya bukan dari WA)
```

---

## 3. `SERVICE_API_KEY` — harus cocok dua arah

Semua panggilan chatbot → backend mengirim header:

```
X-Service-Key: <nilai SERVICE_API_KEY>
```

Nilai ini di sisi chatbot ada di `.env` sebagai `BACKEND_SERVICE_API_KEY`. **Dua-duanya
harus sama persis**, kalau beda semua fitur transaksi bot (buat order, bayar, cek status,
laporan Owner) akan balas `401`. Nilai production disepakati privat — jangan di-commit
ke repo mana pun.

---

## 4. Endpoint backend yang dipakai chatbot (kontrak beku)

Ini yang dipanggil chatbot; **jangan diubah path/bentuk response-nya tanpa kabar-kabari**
(kalau harus berubah, bilang dulu supaya chatbot ikut diupdate barengan).

| Method | Path | Dipakai untuk |
|---|---|---|
| `GET` | `/products/` | Tool `get_menu` (daftar menu, live — tidak pernah di-cache) |
| `GET` | `/products/{id}` | Detail produk + `image_url` |
| `POST` | `/customers` | Simpan/ambil data pelanggan (`nomor_wa`, `nama`, `alamat`) |
| `POST` | `/orders` | Buat pesanan (`customer_id`, `metode_pengiriman`, `created_via`, `items[{product_id, jumlah}]`) |
| `GET` | `/orders/latest?nomor_wa=` | Cek status pesanan terakhir (nomor format lengkap `628…@c.us`) |
| `POST` | `/orders/{id}/cancel` | Batalkan pesanan (stok dikembalikan) |
| `POST` | `/payments` | Charge Midtrans (`{order_id, payment_type: bank_transfer\|qris, amount}`) |
| `GET` | `/payments/{order_id}/status` | Polling pembayaran tiap 30 detik |
| `GET` `POST` | `/customers/{nomor_wa}/takeover` | Human takeover (baca & set) |
| `GET` | `/admin/takeover-handlers` | Daftar nomor admin → `{"numbers": [...]}` |
| `GET` | `/reports/summary?start_date&end_date` | Laporan Owner lewat WA |

Bentuk response yang diandalkan:

- `CustomerOut` / `OrderOut` pakai field `id`; nomor invoice ada di
  `OrderOut.invoice.nomor_invoice`.
- `GET /payments/{order_id}/status` → `{order_id, invoice_status, amount_paid, amount_due, payments[]}`
- `GET /reports/summary` → `{revenue, expenses, order_count, avg_order_value, top_products[{product_id, nama_produk, qty, revenue}]}`

**Chatbot tidak pernah memanggil Midtrans langsung** — semua charge lewat `POST /payments`
di backend, chatbot cuma meneruskan VA/QRIS ke pelanggan dan polling statusnya.

---

## 5. Foto produk: simpan **path relatif**

Kolom `products.image_url` diisi path saja:

```
/static/products/12.jpg
```

Jangan URL absolut (`http://localhost:8001/...`), karena alamat host beda-beda tergantung
pemanggilnya (browser vs container chatbot vs production). Tiap konsumen mem-prefix base
URL-nya sendiri — di chatbot sudah diimplementasi (`BACKEND_BASE_URL` + path), Buyer/Admin
Site juga begitu.

---

## 6. Kalau ada yang aneh

| Gejala | Kemungkinan sebab |
|---|---|
| Push ready → `Connection refused` | Chatbot tidak jalan, atau backend-mu ada di mesin lain (chatbot cuma listen di `127.0.0.1`) |
| Push ready → `404 Not Found` | Salah path. Harus ada prefix `/webhook`: `/webhook/internal/orders/{id}/ready` |
| Push ready → `{"status":"not_found"}` | Bukan error transport. Order-nya memang bukan pesanan dari WhatsApp |
| Chatbot dapat `401` dari backend | `SERVICE_API_KEY` vs `BACKEND_SERVICE_API_KEY` beda |
| Dari dalam container tidak bisa connect ke `localhost` | Di dalam Docker, `localhost` = container itu sendiri. Pakai nama service: `chatbot-service`, `backend` |

Ada pertanyaan atau butuh perubahan kontrak → chat Kevin. Daftar to-do lama +
status verifikasinya ada di `UNTUK_NICHOLAS_backend_todo.txt`.
