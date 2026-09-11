# Fine-Tuning v6 — Toti Cakery Tool-Calling

Spesifikasi dataset v6, disusun dari QA sweep **11 September 2026** terhadap
stack yang berjalan di VM (model aktif saat itu: `toti-qwen-1.7b-v5`).

Polanya sama seperti v4 dan v5: **perilaku yang salah diukur dulu di stack
hidup, baru datanya diperbaiki.** Tidak ada perubahan yang masuk ke sini tanpa
angka di belakangnya.

---

## 1. Apa yang berubah di produk (dan kenapa datanya harus ikut)

Tiga keputusan diambil sesudah QA, dan ketiganya membuat sebagian data v5 justru
mengajarkan hal yang sudah tidak berlaku:

**(A) Keluhan punya balasannya sendiri.** Sampai v5, keluhan pelanggan masuk
`escalate_to_admin`. Sekarang ada tool ke-13, `sampaikan_maaf`, yang
mengembalikan permintaan maaf dengan kalimat tetap. Alasannya terukur: dengan
balasan diserahkan ke model, *"kuenya kemarin basi, aku kecewa banget"* dijawab
**"Wah, makasih banyak kak! Senang banget kalau suka 😊"** — model 1,7 B salah
membaca nada, dan salah nada pada keluhan jauh lebih merugikan daripada salah
memilih tool. Yang tetap jadi keputusan model adalah KAPAN tool itu dipakai.

**(B) Eskalasi menyempit ke pesanan kue custom saja.** Permintaan bicara dengan
orang, nego harga, pesanan besar, dan pertanyaan yang belum ada jawabannya tidak
lagi menawarkan admin. Ini menutup rantai kegagalan yang terukur: tawaran
"mau kusambungkan ke admin?" berhamburan di pertanyaan biasa, lalu pelanggan
menjawab *"makasih ya kak"* — kata "ya" diterima sebagai persetujuan, takeover
menyala, dan bot bungkam sehari penuh.

**(C) Jawaban jumlah polos harus masuk keranjang.** Sesudah bot menampilkan
detail satu kue dan bertanya "mau pesan berapa?", jawaban *"satu aja"* **gagal 6
dari 6 percobaan** di v5: tidak ada tool yang dipanggil dan pelanggan dijawab
"boleh sebutkan nama kuenya?" berulang kali. Sempat ditambal cabang kode, lalu
cabangnya dilepas lagi atas permintaan Kevin — supaya performa model terlihat
apa adanya dan yang diperbaiki datanya, bukan kodenya.

**(D) Klaim harga yang salah tetap dijawab dengan detail produk.** *"brownies
fudgy almond itu 50 ribu kan ya?"* dijawab kalimat ngawur tanpa tool, sehingga
harga salahnya tidak pernah dikoreksi.

---

## 2. Perubahan konkret pada `generate_dataset.py`

| Tipe | v5 | v6 | Isi |
|---|---|---|---|
| **T14** (baru) | — | 45 / 5 / 0 | Keluhan → `sampaikan_maaf(keluhan=…)`: kue basi, telat, salah kirim, tidak sesuai foto, kotak penyok, jumlah kurang |
| **N9** (baru) | — | 35 / 4 / 0 | Dulu dieskalasi, sekarang dijawab sendiri tanpa tool: minta bicara dengan orang, minta nomor telepon, nego harga, diskon borongan |
| **T7** | 25 / 3 / 4 | 55 / 6 / 4 | Templat jawaban jumlah polos di DEPAN pool: `"{qty}{unit} aja"`, `"{qty} dong"`, `"mau {qty}"`, `"just {qty}"` |
| **T3** | 100 / 10 / 9 | 110 / 11 / 9 | Templat klaim harga salah: `"{prod} itu 50 ribu kan ya?"`, `"bukannya {prod} harganya 100 ribu?"` |
| **T10** | 45 / 5 / 4 | 30 / 3 / 4 | Keluhan dan "ngomong sama admin" DIBUANG dari pool; ditambah varian kue custom (bentuk angka, cetak foto, rasa di luar menu) |

(angka = train / validation / test)

**Balasan lama yang menawarkan admin dibersihkan** — kalau tidak, data melawan
prompt: `N2_REPLY_ID/EN` (informasi yang belum ada), `N3_REPLY` ("kalau butuh
admin manusia, bilang aja"), `N6_REPLY` kategori *capacity* (pesanan besar), dan
satu dokumen FAQ pembatalan. Riwayat sintetis jenis `escalate` juga diganti
contohnya jadi permintaan kue custom.

Totalnya: **train 1255 / validation 129 / test 100**.

## 3. Split `test`: 3 baris diselaraskan, 97 tetap

Split `test` dibekukan sejak v1 supaya angka antar-versi sebanding, dan
`generate_dataset.py` memang tidak pernah menulis ulang file itu. Tapi 3 barisnya
mengandung aturan yang sengaja kita ubah: dua keluhan "salah kirim" dan satu
"nego harga order kantor" — semuanya berlabel `escalate_to_admin`. Dibiarkan,
v6 akan dihitung SALAH justru pada perilaku yang diperbaiki.

`patch_test_v6.py` menyelaraskan ketiganya (idempoten, aman dijalankan ulang):
dua jadi `sampaikan_maaf`, satu jadi jawaban teks tanpa tool. **97 baris lain
byte-identik**, jadi perbandingan v3→v4→v5→v6 tetap berlaku dengan catatan kaki
ini.

## 4. Urutan kerja

```bash
# 1. dataset (test otomatis dibekukan dari file lama)
chatbot-service/.venv/bin/python finetune/generate_dataset.py
chatbot-service/.venv/bin/python finetune/patch_test_v6.py

# 2. upload ke HF (v5 sudah diarsipkan di branch v5)
hf upload LasagnaS/toti-cakery-toolcall finetune/data/ data/ --repo-type dataset

# 3. training: jalankan finetune/finetune_toti_qwen3.ipynb di Colab T4 sampai
#    habis — cell terakhir mengunggah GGUF ke repo PUBLIK
#    LasagnaS/toti-qwen-1.7b-v6-gguf

# 4. deploy: entrypoint Ollama di VM menarik sendiri dari HF, tanpa token
#    LLM_MODEL=toti-qwen-1.7b-v6
#    LLM_HF_REF=hf.co/LasagnaS/toti-qwen-1.7b-v6-gguf:Q4_K_M
#    Modelfile.qwen3-1.7b-v6 sudah ada di repo deploy (model/).

# 5. verdict
chatbot-service/.venv/bin/python finetune/eval_tool_calling.py --model toti-qwen-1.7b-v6
chatbot-service/.venv/bin/python finetune/scenario_suite.py     --model toti-qwen-1.7b-v6
```

## 5. Gerbang rilis v6

1. Metrik pada split `test` tidak turun dibanding v5.
2. Enam perilaku baru lolos: keluhan → `sampaikan_maaf`; permintaan bicara
   dengan orang → tanpa tool; nego harga → tanpa tool; jumlah polos sesudah
   detail → `add_to_cart`; klaim harga salah → `get_product_detail`; kue custom
   → tetap `escalate_to_admin`.
3. Tiga suite QA di VM (alur lurus, di luar alur, RBAC) tetap hijau, dan suite
   performa model (D) naik dari 20/22.
4. `pytest chatbot-service/tests/` hijau.

## 6. Jangan dilakukan

- Jangan melatih dari split `test`.
- Jangan mengubah system prompt atau skema tool di dataset "supaya lebih mudah" —
  generator membaca keduanya langsung dari kode runtime, dan paritas itulah
  alasan dataset ini ada.
- Jangan menambah cabang kode baru untuk menutup kegagalan model. Sejak v6,
  jalur deterministik yang sempat dipasang untuk jawaban jumlah polos sudah
  dilepas: kegagalan model harus terlihat di angka QA dan diperbaiki lewat data.
