# Fine-Tuning v8 — Toti Cakery Tool-Calling

Spesifikasi dataset v8, disusun dari QA **24–25 September 2026** di VM
(model aktif: `toti-qwen-1.7b-v7`) dan dari satu percakapan WhatsApp sungguhan
milik Kevin.

Polanya tetap sama seperti v4–v7: **perilaku yang salah diukur dulu di stack
hidup, baru datanya diperbaiki.** v8 sengaja **hanya mengubah data** —
hyperparameter tidak disentuh (2 epoch, lr 2e-4, LoRA r/alpha 16, max_seq 4096),
supaya perbandingan v7 → v8 bersih: kalau ada yang membaik, sebabnya data.

---

## 1. Temuan

**(A) Bahasa balasan bebas ditebak model, bukan diikuti dari sesi.**
Runtime sudah lama memutuskan bahasa percakapan (`bahasa.deteksi()` +
`store.set_lang`, lengket supaya pesan ambigu tidak membalikkannya), tapi
keputusan itu tidak pernah sampai ke model: `SYSTEM_PROMPT` justru menyuruhnya
menebak sendiri dari teks pelanggan tiap giliran. Pada giliran yang **tidak
punya sinyal bahasa sama sekali** tebakannya meleset.

Terukur di VM (pesan `ok` di tengah percakapan Indonesia):

| | bocor ke bahasa Inggris |
|---|---|
| v7, temperature 0,7 | 3 dari 6 |
| v7, temperature 0,3 | 1 dari 13 (7,7%) |

Kalimat yang lolos selalu sama: *"Still here 😊 Type \*menu\* untuk lihat
daftarnya ya"* — baris dataset Inggris yang dihafal model untuk giliran pengisi.

Perbaikan runtime (commit `ca8952f`): `bahasa.arahan(lang)` ditempel ke blok
`TOOL_REMINDER`, jadi blok system kini menyatakan bahasanya. v8 melatih model
dengan bentuk itu — tanpa ini, arahan yang dikirim produksi tidak pernah dilihat
model saat latihan.

**(B) Giliran pengisi hampir tidak terlatih.** Kelas yang gagal justru yang
paling tipis di data: v7 punya 29 giliran pendek (≤2 kata) di tengah
percakapan dari 1.540 baris (1,9%), dan **nol** pesan pengisi polos — setiap
baris "ack" v7 masih membawa kata lain yang menandai bahasanya ("oke sip",
"mantap nih"). Pelanggan sungguhan mengetik `ok`, `oke`, `iya`, `sip`.

**(C) Model v7 mengarang kata.** Di temperature 0,3, 4 dari 16 balasan memuat
kata yang tidak ada: "chat dengan **kubetuk**", "chat dengan **kumu**". v6 pada
uji yang sama: 0 dari 16. Hyperparameter v6→v7 tidak berubah, jadi penyebabnya
ada di data — tapi **belum diketahui pasti bagian mana**. v8 tidak menebak-nebak:
resep dikunci, data diperbaiki, lalu diukur ulang. Kalau masih muncul, barulah
resepnya disetel di v9 dengan bukti.

## 2. Perubahan

1. **Arahan bahasa di blok system tiap baris.** Dirender oleh fungsi runtime
   yang sama (`bahasa.arahan`), dipilih dari `meta.lang` baris itu. Dua bentuk
   blok system yang sah — satu per bahasa — dan `audit_dataset.py` memeriksa
   bahwa bentuknya **cocok dengan meta baris**, bukan sekadar ada.
2. **Giliran pengisi diperbanyak.** N3 50 → 90 baris, N8 35 → 55 baris, dan
   `MT_SHARE["N3"]` 0,3 → 0,6 karena pengisi muncul di TENGAH percakapan —
   di situlah bahasa sesi jadi satu-satunya petunjuk. Pool N3 ditambah pengisi
   polos di kedua bahasa (`ok`, `oke`, `iya`, `sip`, `siap`, `noted`, `k`,
   `sure`, `alright`, …).
   Hasil: giliran pengisi di tengah percakapan **29 → 53** (id 20→38, en 9→15).
3. **Metrik baru di harness eval:** `wrong_language_rate` — bagian balasan teks
   yang meleset dari bahasa sesi. Metrik v7 seluruhnya soal tool, jadi bug yang
   paling terlihat pelanggan tidak bisa tertangkap. Ditambah 3 smoke case
   (v8-1..v8-3) untuk giliran tanpa sinyal bahasa.
4. **`audit_dataset.py`:** 34 → 37 pemeriksaan (paritas arahan bahasa per split).
   Daftar kata fungsi pada pemeriksaan "jawaban N1 bersumber dari konteks"
   ditambah: parafrase pendek yang benar ("Bisa lewat QRIS saja kak — semuanya
   non-tunai") sebelumnya dihitung salah hanya karena "lewat" dan "semuanya"
   tidak muncul di dokumen.

## 3. Ukuran

| split | v7 | v8 |
|---|---|---|
| train | 1.540 | **1.600** |
| validation | 156 | **162** |
| test | 160 | **166** |

Porsi bahasa Inggris tetap: 22% train, 23% validation, 25% test.
Seluruh 37 pemeriksaan audit lolos; dataset deterministik (seed 42).

## 4. Yang TIDAK diubah

- Hyperparameter training, dan seluruh isi notebook selain versi, jumlah baris,
  smoke case, dan metrik bahasa.
- Bentuk prompt lain: konteks FAQ tetap menumpang di pesan pelanggan, reminder
  tetap di ujung blok system, FAQ tetap dilatih sebagai keterampilan membaca.
- Definisi tool dan skemanya.

## 5. Sesudah training

1. Notebook mencetak tabel `perbandingan_v8.md` (v7 vs v8) dan mengunggahnya ke
   repo model HF. Yang dilihat pertama: `wrong_language_rate` dan
   `function_selection_acc` — perbaikan bahasa tidak boleh dibayar dengan
   turunnya ketepatan tool.
2. Deploy di VM: ubah `LLM_MODEL=toti-qwen-1.7b-v8` dan
   `LLM_HF_REF=hf.co/LasagnaS/toti-qwen-1.7b-v8-gguf:Q4_K_M` di `~/toticakery/.env`,
   sediakan `model/Modelfile.qwen3-1.7b-v8`, lalu
   `docker compose up -d --no-deps ollama chatbot-service`.
3. QA VM: ulangi suite pengisi (8 giliran "ok" sesi Indonesia + 6 sesi Inggris)
   dan hitung kata karangan — angka pembandingnya ada di §1.
