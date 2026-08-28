# Konfigurasi backend ⇄ chatbot

Panduan untuk **backend engineer**: apa yang perlu di-set di sisi backend supaya
nyambung dengan Chatbot Service (WhatsApp bot Toti Cakery).

- Cara menjalankan chatbot-nya sendiri → `README.md`
- Status endpoint yang sudah/belum dibangun + permintaan terbuka → `BACKEND_TODO.txt`

Ringkasnya cuma **dua hal** yang perlu di-set di sisi backend: `CHATBOT_URL` dan
`SERVICE_API_KEY`. Selebihnya sudah jalan otomatis.

## 1. `CHATBOT_URL` — isinya apa?

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

## 2. Endpoint chatbot yang dipanggil backend

Cuma **satu**, yaitu push C4 "pesanan siap":

```
POST {CHATBOT_URL}/webhook/internal/orders/{order_id}/ready
```

| Hal | Keterangan |
|---|---|
| Kapan dipanggil | Saat admin menandai order jadi siap (status → *siap/ready*) |
| `order_id` | ID order dari database backend (`orders.id`) — sama dengan yang dikembalikan `POST /orders` |
| Body | **Tidak perlu** (boleh kosong). Chatbot mengambil semua data dari `order_id` |
| Header auth | ⚠️ **WAJIB** (baru, hasil audit keamanan): `X-Internal-Key: <INTERNAL_API_KEY chatbot>`. Nilainya dikirim privat oleh Kevin — simpan di env backend, jangan di-commit. Tanpa header ini balasannya `404` dan pesan WA tidak terkirim. Sebelumnya endpoint ini terbuka, jadi siapa pun yang bisa menjangkau chatbot bisa mem-blast "pesanan siap" ke pelanggan asli dari nomor WA resmi toko. `X-Service-Key` tetap untuk arah sebaliknya (chatbot → backend) |
| Response 200 | `{"status": "ok", "order_id": 12}` → pesan WA sudah dikirim ke pelanggan |
| Response 200 | `{"status": "not_found", "order_id": 12}` → order-nya tidak ada di sisi chatbot (mis. dibuat lewat website, bukan lewat WA). **Bukan error**, tidak usah di-retry terus |
| Idempoten | Ya. Dipanggil berulang untuk order yang sama → pelanggan tetap hanya dikirimi pesan sekali |

Panggilannya sebaiknya *fire-and-forget* — jangan sampai update status di backend
gagal cuma karena chatbot lagi mati. Cukup `try/except` + log.

Cek cepat:

```bash
curl http://localhost:8000/health
# -> {"status":"ok","service":"Toti Cakery Chatbot Service"}

curl -X POST http://localhost:8000/webhook/internal/orders/12/ready \
     -H "X-Internal-Key: $INTERNAL_API_KEY"
# -> {"status":"ok","order_id":12}   (atau "not_found" kalau order-nya bukan dari WA)
```

## 3. `SERVICE_API_KEY` — harus cocok dua arah

Semua panggilan chatbot → backend mengirim header `X-Service-Key: <SERVICE_API_KEY>`.
Di sisi chatbot nilainya ada di `.env` sebagai `BACKEND_SERVICE_API_KEY`. **Dua-duanya
harus sama persis**; kalau beda, semua fitur transaksi bot (buat order, bayar, cek
status, laporan Owner) balas `401`. Nilai production disepakati privat — jangan
di-commit ke repo mana pun.

## 4. Endpoint backend yang dipakai chatbot (kontrak beku)

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

## 5. Foto produk: simpan **path relatif**

Kolom `products.image_url` diisi path saja: `/static/products/12.jpg`. Jangan URL
absolut (`http://localhost:8001/...`) — alamat host beda-beda tergantung pemanggilnya
(browser vs container chatbot vs production). Tiap konsumen mem-prefix base URL-nya
sendiri; di chatbot sudah diimplementasi (`BACKEND_BASE_URL` + path).

## 6. Verifikasi nomor WhatsApp untuk Buyer Site (konsep — belum dibangun)

Usulan pengganti OTP `'7777'`. **Belum ada kontrak final**: desainnya diserahkan
ke backend engineer. Bagian ini cuma menjelaskan idenya dan titik temunya dengan
chatbot, supaya frontend dan backend berangkat dari pemahaman yang sama.

### Idenya: arah pembuktian dibalik

Bukan toko yang mengirim kode ke pelanggan, tapi **pelanggan yang mengirim kode
ke nomor toko** lewat tombol di halaman verifikasi. Tombol itu membuka WhatsApp
dengan pesan yang sudah terisi otomatis (deep link `wa.me`); begitu pelanggan
menekan kirim, chatbot menerimanya dan nomor pengirimnya dijamin asli oleh
WhatsApp.

Kenapa arah ini yang diusulkan: nomor WhatsApp toko sama dengan nomor chatbot,
dan jalan di atas otomasi tidak resmi (wwebjs). Mengirim kode ke nomor yang belum
pernah menghubungi toko adalah pola yang paling gampang dicap spam — kalau nomor
itu kena banned, chatbot ikut mati. Dengan arah terbalik, bot hanya **membalas**
orang yang menyapa duluan, dan tidak perlu membangun pengirim OTP sama sekali
(tanpa SMTP, tanpa SMS gateway, tanpa biaya).

### Aturan yang diminta

Kalau nomor pengirim **berbeda** dengan nomor yang diketik saat registrasi,
verifikasi dan pendaftaran **gagal**. Pelanggan boleh mengirim ulang dari nomor
yang benar, atau mengganti nomornya di form.

### Titik temu dengan chatbot

Chatbot bisa menangkap pesan verifikasi yang masuk lalu memberitahu backend:
*"kode ini datang dari nomor ini"*. Bentuk endpoint dan payload-nya **backend yang
menentukan**; chatbot menyesuaikan, dan memanggilnya dengan `X-Service-Key` sama
seperti endpoint lain.

Satu hal yang perlu disepakati lebih dulu karena paling gampang bikin
pendaftaran gagal padahal nomornya benar: **bentuk kanonik nomor telepon**.
Chatbot menerima nomor dari WhatsApp sebagai `6281234567890@c.us`, sementara user
bisa mengetik `0812-3456-7890`, `+62 812 3456 7890`, atau `62812…`. Selama kedua
sisi tidak menormalkan dengan aturan yang sama, verifikasi akan gagal secara
acak.

### Untuk Buyer Site

Yang pasti dibutuhkan halaman verifikasi: **tombol yang membuka WhatsApp** dengan
tautan yang diberikan backend, **jalur cadangan untuk pengguna desktop** (tampilkan
nomor toko + kodenya supaya bisa dikirim manual dari HP — deep link tidak berguna
kalau WhatsApp Web belum login), dan **cara mengetahui kapan verifikasi berhasil**
(polling atau semacamnya, sesuai yang nanti disediakan backend). Beri keterangan
juga bahwa nomor itu bot pemesanan, bukan nomor CS.

### Prasyarat, berlaku untuk solusi apa pun

`VERIFIED_TOKENS` di `app/services/buyer_auth_service.py` masih `dict` di memori
proses. Alur verifikasi yang melibatkan dua service akan rapuh selama token itu
hilang tiap restart dan tidak terlihat oleh worker lain. Detail di
`BACKEND_TODO.txt` #4.

> Begitu backend menetapkan kontraknya, tulis di sini supaya chatbot dan Buyer
> Site punya satu rujukan.

## 7. Kalau ada yang aneh

| Gejala | Kemungkinan sebab |
|---|---|
| Push ready → `Connection refused` | Chatbot tidak jalan, atau backend ada di mesin lain (chatbot cuma listen di `127.0.0.1`) |
| Push ready → `404 Not Found` | Salah path (harus ada prefix `/webhook`: `/webhook/internal/orders/{id}/ready`), **atau** header `X-Internal-Key` hilang/salah — auth yang gagal sengaja dibalas `404`, bukan `401`, biar endpoint-nya tidak bisa dipetakan orang luar |
| Push ready → `{"status":"not_found"}` | Bukan error transport. Order-nya memang bukan pesanan dari WhatsApp |
| Chatbot dapat `401` dari backend | `SERVICE_API_KEY` vs `BACKEND_SERVICE_API_KEY` beda |
| Dari dalam container tidak bisa connect ke `localhost` | Di dalam Docker, `localhost` = container itu sendiri. Pakai nama service: `chatbot-service`, `backend`, `ollama` |

Daftar to-do backend lama + status verifikasinya ada di `BACKEND_TODO.txt`.
