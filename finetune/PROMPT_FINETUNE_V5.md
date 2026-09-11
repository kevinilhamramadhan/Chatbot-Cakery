# PROMPT: Fine-tuning Toti Cakery v5 — baca file ini dulu sebelum mulai

> Untuk sesi berikutnya. v4 sudah jalan di produksi; file ini hanya berisi apa
> yang v4 masih salah menurut pengukuran, dan cara menutupnya. Pelajaran lama
> tetap berlaku — `PROMPT_FINETUNE_V4.md` masih wajib dibaca (§1 infrastruktur,
> §4 jebakan training, §6 quantize). Jangan ulangi yang sudah dibayar mahal.

## 0. Status masuk v5

- Model produksi: **`toti-qwen-1.7b-v4`** (Qwen3 1.7B + LoRA, GGUF Q4_K_M).
- Dataset v5 **sudah digenerate**: `finetune/data/` train **1150** / val **118**
  / test **100 (beku sejak v1)**. Regenerate: `python finetune/generate_dataset.py`.
- Gerbang rilis v5 **sudah ada dan sudah dijalankan pada v4** sebagai baseline:
  `finetune/regression_v5.py` → **GAGAL** (lihat §2). Itu memang yang diharapkan;
  angka itulah yang harus dilewati v5.

```
akurasi routing      : 40/52 = 77%   (gerbang >= 95%)
order -> escalate    : 4             (gerbang = 0)
riwayat tidak stabil : 1             (gerbang = 0)
nama tool bocor      : 0             (gerbang = 0)
```

Angka 77% ini lebih rendah dari 89% yang sempat tercatat karena kasus ujinya
diperluas dari "alur yang benar" ke perilaku user yang sebenarnya (§1b). Itu
bukan model yang memburuk — itu pengukuran yang berhenti menyanjung diri.

## 1. Kegagalan terukur yang harus ditutup v5

Semua dari QA sweep di stack live, 3 September 2026 (75 giliran percakapan +
46 giliran uji routing, model v4, backend & Ollama asli). Bukan tebakan.

**A. Order generik tanpa jumlah diteruskan ke admin.**
`"mau order cupcake dong"` → `escalate_to_admin`, **3 dari 3** run, dengan
riwayat apa pun. `"bungkusin bento cookies 3 dong"` juga. Akar masalahnya
lexical: kalimat itu lebih dekat ke template T10 (`"mau pesan wedding cake …
bisa?"`, `"mau nego harga buat order kantor jumlah besar"`) daripada ke T5, yang
di v4 hampir selalu membawa angka. Efeknya paling parah: pelanggan dijanjikan
admin untuk pesanan biasa.

**B. Satu balasan handover di riwayat menular ke pesan berikutnya.**
`"aku mau bento cookies 2"` benar `add_to_cart` 3/3 pada riwayat kosong, tapi
`escalate_to_admin` **3/3** begitu balasan escalate ada di window. Sekali salah,
seterusnya salah — di skenario live pelanggan mengulang pesanan yang sama dan
di-escalate lagi.

**C. Pertanyaan harga satu produk bernama dirutekan ke `get_menu`.**
`"brp harga brownies?"` dan `"bento cookies harganya berapa?"` → `get_menu`
dengan `kategori` karangan. Seharusnya `get_product_detail` (harga + foto).

**D. Balasan menyebut nama tool dan produk karangan.**
`"mau donat gula 6"` → *"Kalau mau lihat pilihan lengkapnya dulu, aku bisa
panggil get_menu. Mau coba?"* — nama tool bocor ke pelanggan.
`"halo masih ada?"` → *"mana yang lebih enak antara cupcakes klasik dan cupcakes
karamel?"* — dua produk yang tidak ada di katalog.

## 1b. Cakupan use case — user tidak mengikuti alur yang kita buat

Audit cakupan (3 Sep 2026) menemukan v4 hanya terlatih pada percakapan yang
"berperilaku baik". Delapan kelas perilaku nyata tidak punya satu pun contoh
latih. Semua sudah ditambahkan; kolom terakhir adalah yang benar-benar terjadi
saat diketik ke bot v4.

| Kelas perilaku | Perilaku benar | v4 melakukan |
|---|---|---|
| Injeksi prompt / menimpa instruksi | tolak, teks, **tanpa** tool | `escalate_to_admin` |
| Rahasia dapur (resep, modal, supplier) | tolak sopan, teks | `get_product_detail` |
| Pertanyaan meta soal bot (model apa, system prompt) | jawab singkat, teks | — |
| Sapaan telanjang (`kak`, `min`, `halo?`) | sapa balik | penolakan out-of-scope |
| Tidak sabar (`kok gak dibales sih`) | yakinkan, tawarkan bantuan | jawaban ngawur |
| Pesan barang di luar katalog (`donat gula`) | `add_to_cart` apa adanya | tidak ada tool + bocor nama tool |
| Pesanan borongan (`50 box`) vs nego harga | `add_to_cart` vs `escalate` | `escalate` untuk keduanya |
| Jumlah kabur (`beberapa`, `banyak`) | tanya jumlahnya | menebak angka |

Yang ditambahkan ke generator:

- **N7** (45 baris) — injeksi / rahasia dapur / meta. Kategori diselang-seling
  di pool karena `pool_split` menyisihkan 15% template terakhir sebagai
  test-only; urutan lama membuat seluruh kategori `meta` tidak pernah terpakai.
  Self-check baru memastikan tiap kategori muncul di train.
- **N8** (35 baris) — sapaan telanjang + pesan tidak sabar.
- **T13** (40 baris) — klaim "sudah saya bayar" → `check_payment_status`.
- **T5** — ±13% barang di luar katalog, ±8% jumlah borongan (25–100).
- **N5** — kategori `vagueqty`: produk jelas, jumlah kabur → tanya, jangan tebak.
- **T1** — pesan dengan dua pertanyaan sekaligus.
- **T3** — nama produk telanjang (`"bolu pandan"` tanpa kalimat tanya).
- **N2** — pertanyaan diskon/promo (jujur: belum ada datanya → tawarkan admin).

**Catatan jujur:** menambah tool `check_payment_status` juga MEMPERLEBAR ruang
salah — `"kok gak dibales sih"` sekarang bisa jatuh ke tool itu (dan memang
terjadi pada v4). Itulah sebabnya N8 ada, dan sebabnya kasus itu masuk gerbang.

## 2. Yang SUDAH dikerjakan (jangan diulang)

| Perubahan | Berkas |
|---|---|
| Kind riwayat `escalate` + penanda runtime di dataset | `generate_dataset.py` (`h_escalate`, `_hist_for`) |
| Template T5 tanpa jumlah / generik (`"mau order {prod} dong"` dst.) | `generate_dataset.py` (`T5_ID`, `T5_ID_NOQTY`, `T5_EN`) |
| Template T3 pertanyaan harga satu produk | `generate_dataset.py` (`T3_ID_V5`, `T3_EN_V5`) |
| Komposisi: T5 140→170, T3 90→100, **T10 60→45**, MT naik di T5/T6/T8 | `generate_dataset.py` (`TRAIN_COUNTS`, `MT_SHARE`) |
| Self-check: balasan teks dilarang memuat nama tool / nama produk katalog | `generate_dataset.py` (`self_check`) |
| Self-check: dataset wajib memuat baris "order normal sesudah handover" | `generate_dataset.py` (`_check_v5_history`) |
| Kompresi balasan escalate di riwayat runtime (paritas train-serve) | `chatbot-service/app/llm/agent.py` (`_history_view`) |
| N7/N8/T13 + T5 luar-katalog & borongan + N5 `vagueqty` (§1b) | `generate_dataset.py` |
| Self-check: tiap kategori N7/N8 wajib terpakai; T5 wajib punya luar-katalog & borongan | `generate_dataset.py` (`_check_v5_coverage`) |
| Routing statis dicabut: `check_payment_status` jadi tool, klasifikasi niat di guard harga dihapus | `chatbot-service/app/tools/payment_status.py`, `app/llm/agent.py` |
| Harness gerbang rilis (52 kasus routing + 12 kasus riwayat) | `finetune/regression_v5.py` |
| Notebook Colab v5 (smoke A–D, assert 1030/105, nama GGUF `.v5`) | `finetune_toti_qwen3.ipynb` |
| Modelfile v5 | `Modelfile.qwen3-1.7b-v5` |

Bukti kind riwayat baru benar-benar terpakai (output generator):
`train: 71 baris sesudah handover, 44 di antaranya memesan normal`.

**Catatan penting soal T10 turun 60→45.** Escalate di v4 bukan kurang terlatih,
tapi **kelewat sering menyala**. Dua kasus custom yang sah tetap dirutekan 2/2
di sweep, jadi porsinya aman diturunkan. `regression_v5.py` menjaga arah
sebaliknya: tiga kasus escalate yang sah ikut diuji, jadi kalau v5 kebablasan
dan berhenti meng-escalate kue custom, gerbangnya ikut gagal.

## 3. Langkah menjalankan v5

1. **Regenerate + upload dataset.** `python finetune/generate_dataset.py` (harus
   cetak `self-check OK` dan dua baris `v5 history check`). Lalu push
   `finetune/data/` ke `LasagnaS/toti-cakery-toolcall`. **Kalau langkah upload
   dilewat, notebook melatih data v4** — assert 1030/105 di notebook yang akan
   menangkapnya.
2. **Baseline dulu, sebelum training.** Jalankan `regression_v5.py` pada v4 dan
   simpan hasilnya. Tanpa baseline, "lebih baik" tidak bisa dibuktikan.
3. **Training**: `finetune_toti_qwen3.ipynb` di Colab T4, base
   `unsloth/Qwen3-1.7B` (jangan qwen3.5:0.8b — lihat v4 §4). Export GGUF
   Q4_K_M → `toti-qwen-1.7b.Q4_K_M.gguf.v5`, push ke HF (GGUF **dilarang** masuk git).
4. **Import ke Ollama** — Ollama adalah container, model store-nya named
   volume `ollama_models`; tidak ada Ollama di host. Taruh GGUF di `finetune/`
   (di-mount read-only ke `/finetune` di container ollama), lalu:
   ```bash
   # cara termudah, idempoten, sekaligus mengurus embedding + stack:
   LLM_MODEL=toti-qwen-1.7b-v5 ./scripts/bootstrap.sh
   # atau manual:
   sed 's#^FROM .*#FROM /finetune/toti-qwen-1.7b.Q4_K_M.gguf.v5#' \
       finetune/Modelfile.qwen3-1.7b-v5 > finetune/Modelfile.generated
   docker compose exec -T ollama ollama create toti-qwen-1.7b-v5 \
       -f /finetune/Modelfile.generated
   ```
   `bootstrap.sh` menurunkan nama Modelfile dari `LLM_MODEL`, jadi keduanya
   tidak bisa lepas sinkron.
5. **Eval** (semua tiga, urut):
   ```bash
   python finetune/eval_tool_calling.py --model toti-qwen-1.7b-v5
   python finetune/scenario_suite.py     --model toti-qwen-1.7b-v5
   python finetune/regression_v5.py      --model toti-qwen-1.7b-v5
   ```
6. **Deploy**: `LLM_MODEL=toti-qwen-1.7b-v5` di `.env` root →
   `docker compose up -d --build chatbot-service`.

## 4. Gerbang rilis v5 (jangan dilonggarkan)

Lulus = **keempat** syarat di bawah, DAN metrik harness v4 tidak turun.

| Syarat | Ambang | Alat |
|---|---|---|
| Akurasi routing | ≥ 95% (v4: 77%) | `regression_v5.py` |
| Pesanan jatuh ke escalate | 0 (v4: 4) | `regression_v5.py` |
| Riwayat tidak stabil | 0 (v4: 1) | `regression_v5.py` |
| Nama tool bocor ke pelanggan | 0 (v4: 0) | `regression_v5.py` |
| Metrik BFCL | ≥ jangkar v4 | `eval_tool_calling.py` |
| Perilaku per tipe | tidak turun | `scenario_suite.py` |

Sampling ikut produksi (`config.py`: temperature 0.7, top_p 0.8, num_ctx 32768,
thinking ON) — `regression_v5.py` memakai `run_agent()` produksi apa adanya,
hanya tool-nya yang di-stub. Skor turun → **jangan rilis**, perbaiki dataset,
ulangi.

## 5. Yang v5 TIDAK tangani (sengaja)

- **Ambang RAG.** `"eh bentar, kalian buka jam berapa?"` (0,390) ditolak padahal
  `"kalian buka jam berapa?"` (0,457) diterima; `"ongkir berapa ya?"` 0,310.
  Ini soal model embedding + cara FAQ di-index, bukan LLM-nya — masuk gelombang
  terpisah, jangan dicampur ke dataset ini.
- **Verifikasi nomor WhatsApp** masih mati by default (endpoint backend belum ada).
- **Perbaikan sisi kode** dari QA sweep yang sama (takeover 404, langkah
  identitas, filter kategori, qty, e-wallet, race sesi) **sudah dikerjakan
  terpisah** dan tercakup `tests/test_qa_regressions.py`. Jangan mencoba
  mengajarkan hal-hal itu ke model — semuanya deterministik di kode.
