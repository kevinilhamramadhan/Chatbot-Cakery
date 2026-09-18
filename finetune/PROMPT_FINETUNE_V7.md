# Fine-Tuning v7 — Toti Cakery Tool-Calling

Spesifikasi dataset v7, disusun dari QA ujung-ke-ujung **18–19 September 2026**
di VM (model aktif: `toti-qwen-1.7b-v6`, 167 pesan, suite S/W/R/B).

Polanya tetap: **perilaku yang salah diukur dulu di stack hidup, baru datanya
diperbaiki.** Bedanya dengan v4–v6: kali ini sebagian besar masalah bukan kurang
contoh, melainkan **bentuk data yang tidak sama dengan yang diterima model di
produksi.**

---

## 1. Temuan

**(A) Letak konteks FAQ.** Sejak 14 Agustus runtime menempelkan FAQ hasil RAG ke
PESAN pelanggan, bukan ke system block (supaya prefix KV-cache Ollama stabil):

```
KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):
Q: … A: …

Pertanyaan pelanggan: lapis legit premium 1
```

Di QA 19 Sep, 136 dari 208 giliran model (65%) membawa blok ini. Dataset v6:
hanya 90 dari 1255 baris (7%, semuanya N1) — dan ditaruh di **system block**.
Model hampir tidak pernah dilatih dengan bentuk pesan yang sebenarnya ia terima.
Gejala di QA yang cocok dengan ini: "lapis legit premium 1" dijawab detail
produk, jawaban aneh berisi penanda `#FAQ_Bukaan` / `#FAQ_Toko`.

**(B) Dua tool tanpa satu pun baris latihan.** `check_cart` dan
`resend_payment_method` ada di runtime sejak v5, tetapi generator tidak pernah
membuat barisnya. QA W4: "pesananku yang kemarin gimana?" → `check_cart`
("keranjangmu masih kosong"); "belum sempat bayar, masih bisa?" → dijawab
"nggak bisa". Audit v6 (22 pemeriksaan) tidak menangkap ini karena tidak ada
pemeriksaan cakupan per tool.

**(C) Argumen yang tidak ada di skema.** `get_menu` sudah tidak berparameter,
tapi 30 baris T2 v6 masih melatih `{"kategori": …}`.

**(D) Kasus QA lain:** bukan-Owner minta laporan dijawab kalimat umum; "ganti
ke transfer bank" dijawab dengan alasan yang salah; "order" dan "oke gas"
memicu `cancel_order`; "do you speak english?" dijawab "We speak Bahasa
Indonesia, sorry"; "kalau ambil besok jam 10 bisa?" dijawab "bisa banget" tanpa
dasar; "lapis legit" terdaftar sebagai barang DI LUAR katalog padahal sekarang
produk nyata.

## 2. Prinsip FAQ: keterampilan, bukan hafalan

Isi FAQ disunting lewat Admin Site dan dibaca chatbot lewat RAG. Yang dilatih
adalah **cara membaca konteks**, bukan isinya — kalau isinya yang dihafal, model
tetap mengulang fakta lama sesudah FAQ diubah, dan RAG kehilangan gunanya.

`faq_topik.py`: 14 topik (+2 khusus test) dengan **slot fakta** yang diacak per
baris — jam buka, daya tahan, kurir, metode bayar, batas bayar, persen DP,
halal, H-x kue custom, lokasi, pesan hari-H, pesanan besar, alergen, website,
ongkir. Dokumen dan jawaban acuan dirender dari slot yang sama, jadi pertanyaan
yang sama punya jawaban berbeda di baris berbeda (**fakta tandingan**). Yang
tidak diacak: cara kerja bot sendiri ("ketik batal", dsb.).

- **N1** (150): konteks memuat jawaban → jawab dari konteks (grounded ≥ 60% kata).
- **N1x** (40): konteks ada tapi tidak memuat jawaban → jujur belum tahu.
- **N2**: hal di luar semua FAQ, dengan dokumen lain sebagai konteks.
- Baris lain: 65% membawa 1–3 dokumen acak (model belajar mengabaikan konteks
  yang tidak relevan dan tetap memanggil tool).
- 15% dokumen konteks diambil dari 16 FAQ asli VM (`faq_asli_vm.json`) sebagai
  contoh format — tidak pernah sebagai jawaban N1/N1x.
- Topik `retur` dan `kemasan` hanya di split test: kalau model benar di sana,
  ia membaca, bukan mengenali.

## 3. Perubahan `generate_dataset.py`

| Tipe | v6 | v7 | Isi |
|---|---|---|---|
| **T15** (baru) | — | 35 | isi keranjang → `check_cart` |
| **T16** (baru) | — | 40 | kirim ulang kode bayar / "belum sempat bayar" → `resend_payment_method`, selalu sesudah tagihan terbit |
| **N1x** (baru) | — | 40 | konteks tanpa jawaban → jujur belum tahu |
| **N10** (baru) | — | 25 | bukan Owner minta laporan → teks (tool Owner tidak dimuat) |
| **N11** (baru) | — | 20 | ganti cara bayar sesudah tagihan → teks: batal lalu pesan ulang |
| **N12** (baru) | — | 30 | "order", "oke gas", "1", "…" tanpa keranjang → tanya balik, tanpa tool |
| **N13** (baru) | — | 15 | bahasa percakapan ("do you speak English?") |
| N1 | 90 | 150 | FAQ dari konteks, slot acak |
| T5 | 170 | 180 | + pesanan ringkas "{produk} {angka}" |
| T3 | 110 | 115 | + minta foto dengan kata umum |
| T8 | 50 | 55 | + history tagihan (pasangan keras T15) |
| T2 | `get_menu(kategori)` | `get_menu()` | skema runtime |

Juga: 6 produk nyata masuk menu (Brownies Fudgy Almond, Brownies Coklat, Lapis
Legit Premium, Chiffon Cake Pandan, Bolu Pandan, Cupcake Bunga); "lapis legit"
keluar dari daftar barang luar katalog; balasan checkout di history memakai
templat `bahasa.py` dan cara bayar sesuai pilihan pelanggan; N2 "COD" dan "luar
kota" dipindah ke FAQ (topik `bayar`/`kirim`).

Totalnya **train 1540 / validation 156 / test 160**. Split test **digenerate
ulang** (tidak lagi dibekukan sejak v1), ±10% train per tipe, dari potongan
template, produk, dan topik FAQ khusus test. `patch_test_v6.py` tidak dipakai
lagi.

Satu perubahan di kode chatbot supaya paritas tidak bisa bergeser diam-diam:
`app/llm/agent.pertanyaan_dengan_konteks()` — runtime dan generator memanggil
fungsi yang sama.

## 4. Audit

`audit_dataset.py` ditulis ulang: 34 pemeriksaan terhadap **kode runtime**
(bukan terhadap generator). Hasil:

- dataset v7: **34/34 lolos**
- dataset v6: **16 gagal** — system block, porsi & letak konteks FAQ,
  parameter `kategori` karangan, `check_cart`/`resend_payment_method` nol baris,
  tiga tool tidak diuji di test, jawaban N1 tidak bersumber dari konteks.

## 5. Notebook

`finetune_toti_qwen3.ipynb` v7:

- sanity check mengikuti bentuk v7 (system identik, konteks hanya di pesan
  terakhir, porsi konteks ±65%);
- eval memakai baris test apa adanya (sudah berbentuk runtime), cap **192 token**
  = `LLM_NUM_PREDICT` produksi;
- dua metrik baru: `faq_grounded_acc` (N1: fakta kunci konteks ada di jawaban)
  dan `faq_jujur_acc` (N1x: mengaku belum tahu);
- **tabel sebelum vs sesudah** (metrik agregat + per tipe + contoh yang
  diperbaiki) dicetak di output, disimpan ke `perbandingan_v7.md/.json`, dan
  di-upload ke repo model HF bersama GGUF (juga tertanam di README model card).

## 6. Urutan kerja

```bash
# 1. dataset + audit
chatbot-service/.venv/bin/python finetune/generate_dataset.py
chatbot-service/.venv/bin/python finetune/audit_dataset.py finetune/data

# 2. upload ke HF (v6 diarsipkan dulu di branch v6)
hf upload LasagnaS/toti-cakery-toolcall finetune/data/ data/ --repo-type dataset
hf upload LasagnaS/toti-cakery-toolcall finetune/README.md README.md --repo-type dataset

# 3. Colab T4: finetune_toti_qwen3.ipynb dari atas ke bawah — cell terakhir
#    meng-upload GGUF + tabel perbandingan ke LasagnaS/toti-qwen-1.7b-v7-gguf

# 4. deploy: .env VM
#    LLM_MODEL=toti-qwen-1.7b-v7
#    LLM_HF_REF=hf.co/LasagnaS/toti-qwen-1.7b-v7-gguf:Q4_K_M
#    docker compose up -d --no-deps ollama chatbot-service
```

## 7. Gerbang rilis v7

1. Semua target di tabel notebook tercapai (fungsi ≥ 0,85, argumen ≥ 0,70,
   tanpa-tool ≥ 0,85, false-tool ≤ 0,10, FAQ grounded ≥ 0,70, FAQ jujur ≥ 0,70).
2. QA ujung-ke-ujung di VM (suite S/W/R/B) tidak lebih buruk dari v6, dan kasus
   QA 18–19 Sep di atas lolos.
3. `pytest chatbot-service/tests/` hijau.
