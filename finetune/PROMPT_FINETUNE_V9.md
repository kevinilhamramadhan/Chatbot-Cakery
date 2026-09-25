# Fine-Tuning v9 — Toti Cakery Tool-Calling

Spesifikasi dataset v9. Disusun dari **kegagalan v8 yang terukur di VM**
(25 September 2026), bukan dari harapan.

v9, seperti v8, **hanya mengubah data**. Hyperparameter tetap tidak disentuh
(2 epoch, lr 2e-4, LoRA r/alpha 16, max_seq 4096).

---

## 1. Apa yang salah di v8

v8 dibuat untuk menutup satu hal: sesi berbahasa Indonesia yang dijawab bahasa
Inggris pada giliran pengisi ("ok"). Caranya: arahan bahasa ditempel di blok
system tiap baris, DAN jumlah baris pengisi dinaikkan (N3 50→90, N8 35→55,
MT_SHARE N3 0,3→0,6) dengan porsi Inggris tetap 25%.

Hasil A/B di VM, 25 sesi per model, pertanyaan identik:

| | v7 | v8 |
|---|---:|---:|
| Balasan sesi Indonesia yang bercampur Inggris | **1/25 (4%)** | **8/25 (32%)** |
| Kata karangan ("kubetuk", "kumu") | 2/25 | **0/25** |

**v8 delapan kali lebih buruk pada hal yang justru mau diperbaiki.** Yang lolos
selalu berbentuk sama: *"Okay, I'm ready to help! 😊"* lalu lanjut Indonesia.

Sebabnya bisa dilacak ke perubahannya sendiri: menaikkan N3 dengan porsi
Inggris tetap membuat contoh **pembuka Inggris untuk giliran pengisi** naik dari
±4 ke ±15 baris. Model tidak diajari KAPAN frasa itu boleh dipakai — hanya
diberi lebih banyak frasa siap pakai untuk jenis giliran yang memang rawan.

**Metriknya juga menipu.** `wrong_language_rate` v8 = 0,000, padahal 8 dari 25
balasan bercampur. Detektornya hanya mencari kalimat Inggris UTUH ("Still
here…"), jadi balasan campur lolos sebagai benar.

## 2. Perubahan v9

1. **Penambahan v8 di N3/N8 DIKEMBALIKAN** ke nilai v7 (N3 50, N8 35,
   MT_SHARE N3 0,3), termasuk pengisi polos yang v8 tambahkan ke pool N3.
   Beban pengisi pindah seluruhnya ke tipe baru di bawah.
2. **Tipe baru N14 — pengisi berpasangan kontras** (60 baris train, 30 pasang;
   6 val, 6 test). Satu pasang = dua baris yang **isinya sama persis kecuali
   bahasanya**:
   - teks pelanggan sama (12 dari 30 pasang memakai token yang **identik
     byte-per-byte**: `ok`, `hmm ok`, `k`, `noted`),
   - pembuka percakapan sama isinya, beda bahasa,
   - balasan acuan dalam bahasa masing-masing.

   Karena kata terakhirnya tidak menandakan bahasa apa pun, satu-satunya cara
   menjawab benar adalah **membaca arahan bahasa dan bahasa history**. Itu yang
   tidak pernah dilatih di v7 maupun v8.
3. **N14 tidak pernah membawa konteks FAQ** (`KONTEKS_SHARE["N14"] = 0`):
   pesan "ok" tidak lolos ambang RAG di runtime, dan konteks yang berbeda di
   kedua sisi pasangan justru merusak kontrasnya.
4. **Detektor bahasa di harness eval diperbaiki**: mencari KATA Inggris di mana
   pun dalam balasan, bukan frasa utuh; serapan yang wajar dipakai pelanggan
   Indonesia (menu, order, chat, ok) sengaja tidak dihitung. Dengan detektor
   lama, kegagalan v8 tidak akan pernah terlihat di tabel Colab.
5. **`audit_dataset.py` 37 → 41 pemeriksaan**: N14 seimbang id/en, tidak pernah
   ber-konteks, selalu ber-history, dan balasan sisi Indonesia bebas frasa
   pembuka Inggris. Pemeriksaan "teks pelanggan unik" dan "tidak bocor antar
   split" kini mengecualikan N14 — teks yang berulang di situ adalah inti
   desainnya, bukan cacat.

## 3. Ukuran

| split | v7 | v8 | v9 |
|---|---|---|---|
| train | 1.540 | 1.600 | **1.600** |
| validation | 156 | 162 | **162** |
| test | 160 | 166 | **166** |

Sama dengan v8 karena 60 baris yang dikembalikan dari N3/N8 digantikan 60 baris
N14. Seluruh 41 pemeriksaan audit lolos; deterministik (seed 42).

## 4. Cara menilai v9 — jangan ulangi kesalahan v8

Tabel Colab **tidak cukup**. Yang menentukan adalah A/B di VM dengan prompt yang
sama untuk v7 dan v9, memakai detektor kata (skrip yang sama seperti 25 Sep):

- **Syarat lulus:** balasan bercampur Inggris di sesi Indonesia **≤ 1/25**,
  yaitu tidak lebih buruk dari v7.
- Kata karangan: v7 2/25, v8 0/25. v9 diharapkan tetap 0.
- `faq_grounded_acc` tidak boleh turun di bawah 0,70 (v7 0,867; v8 0,533).
- `false_tool_rate` tidak boleh di atas 0,10 (v7 0,097; v8 0,192).

Kalau v9 tidak lulus syarat pertama, **berhenti menambah data**: masalahnya
bukan jumlah contoh, dan penutupnya adalah jalur deterministik — pesan pengisi
dibalas dari templat `bahasa.py` tanpa memanggil model sama sekali.
