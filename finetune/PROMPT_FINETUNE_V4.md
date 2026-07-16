# PROMPT: Fine-tuning Toti Cakery v4 — baca file ini dulu sebelum mulai

> Untuk sesi Claude Code / engineer berikutnya. Semua pelajaran dari v1–v3 dan
> dari tes live WhatsApp (15–16 Jul 2026) terkumpul di sini. Ikuti urutannya;
> jangan ulangi kesalahan yang sudah dibayar mahal.

## 0. Konteks singkat

- Model produksi sekarang: **`toti-qwen-1.7b`** (Qwen3 1.7B + LoRA via Unsloth,
  ekspor GGUF Q8_0, di-serve Ollama dalam container `toti-ollama`).
- Skor v3: **5/5 target harness** (lihat `results_toti-qwen-1.7b.v2.json`) —
  tapi tes live WhatsApp menemukan kegagalan yang TIDAK tertangkap harness
  (lihat §2). v4 ada untuk menutup itu.
- Arsitektur chatbot: tool output dikirim VERBATIM ke pelanggan (model tidak
  pernah melihat hasil tool). Jadi tugas model HANYA: (a) memilih tool +
  argumen, atau (b) menjawab langsung untuk sapaan/FAQ. Tidak ada tugas lain.

## 1. Infrastruktur yang SUDAH ada (pakai, jangan bikin baru)

| File | Guna |
|---|---|
| `generate_dataset.py` | Generator dataset sintetis (perbaiki sesuai §3, jangan tulis ulang) |
| `finetune_toti_qwen3.ipynb` | Notebook training Unsloth (Colab T4) — alur yang terbukti jalan |
| `eval_tool_calling.py`, `scenario_suite.py` | Harness eval 18 skenario × 3 run |
| `wired_test.py` | Tes wiring end-to-end lewat kode chatbot asli |
| `Modelfile.qwen3-1.7b` | Template import GGUF ke Ollama |
| `INSTRUKSI_FINETUNING.md`, `BASELINE.md` | Prosedur & angka baseline lama |

## 2. PELAJARAN DARI PRODUKSI — kegagalan live yang harus ditutup v4

Empat insiden nyata di WhatsApp (semua sudah ditambal di sisi kode/prompt,
tapi akar model-nya baru benar-benar sembuh lewat dataset v4):

1. **Halusinasi menu dari hafalan training.** Ditanya "ada menu apa saja?",
   model menjawab menu 2 item dengan harga FIKTIF (Bento Rp110.000, "Cake 10cm
   Vanilla" Rp100.000) TANPA memanggil `get_menu`. Harga fiktif itu berasal
   dari dataset sintetis lama yang menaruh isi menu literal di jawaban asisten
   → model menghafalnya. **Ini bug dataset, bukan bug model.**
2. **Salah routing detail→menu.** "bento cookies kayak gimana ya?" dijawab
   `get_menu` berulang kali, apalagi saat history berisi jawaban menu.
3. **Meniru history alih-alih memanggil tool.** Saat history berisi jawaban
   detail yang benar, model menyalin teksnya verbatim tanpa tool call (teks
   tampak benar; foto tidak terkirim, harga berisiko basi).
4. **Produk ambigu ditebak.** "beli 4 cupcake" → model memilih varian sendiri.
   (Kini resolver kode yang menangani ambiguitas — model cukup meneruskan kata
   pelanggan apa adanya sebagai argumen.)

## 3. ATURAN DATASET v4 (bagian terpenting file ini)

1. **JANGAN PERNAH menaruh isi menu/harga/deskripsi produk di jawaban asisten.**
   Contoh training untuk pertanyaan data HARUS berhenti di tool call:
   `user: "menu apa aja?" → assistant: <tool_call>get_menu{}</tool_call>` — SELESAI.
   Tidak ada turn lanjutan berisi daftar kue. Model tidak boleh punya satu pun
   contoh "menjawab menu dengan teks" untuk dihafal. (Akar insiden #1.)
2. **Perbanyak varian frasa detail-produk** → `get_product_detail`:
   "X kayak gimana / seperti apa / kaya apa / bentuknya gimana / ada fotonya /
   X itu apa / ceritain dong X", campur id/en, dengan X beragam. (Insiden #2.)
3. **Argumen = kata-kata pelanggan apa adanya.** "beli 4 cupcake" →
   `add_to_cart {items:[{product:"cupcake", qty:4}]}` — model DILARANG
   mengarang nama lengkap varian. Resolver kode yang memutuskan/bertanya.
   Sertakan contoh-contoh generik seperti ini. (Insiden #4.)
4. **Format history harus meniru produksi.** Produksi kini mengganti balasan
   tool lama di history dengan penanda ringkas, mis.
   `[Aku sudah menampilkan daftar menu via tool get_menu]`. Sebagian contoh
   dataset harus memuat history berpenanda seperti ini, lalu user bertanya
   lagi → asisten TETAP memanggil tool (bukan meniru/merujuk history).
   (Insiden #3.)
5. **Susun ulang system prompt contoh training = system prompt produksi**
   (`app/llm/prompt.py`) + reminder dekat-pertanyaan (`app/llm/agent.py`,
   SystemMessage kedua). Format pesan harus sama persis dengan runtime.
6. **Data mixing tetap wajib** (C300 §fine-tuning): campur percakapan umum
   (sapaan, terima kasih, out-of-scope yang ditolak sopan) supaya tidak
   catastrophic forgetting. Out-of-scope → tolak, TANPA tool call.
7. Tool baru sejak v3 yang harus punya contoh: `financial_report` /
   `business_analytics` (khusus Owner; non-owner tetap boleh memanggil —
   gating ada di kode), dan alur channel pembayaran TIDAK melibatkan model
   (deterministik) — jangan buat contoh yang mengajarkan model menanyakan
   VA/QRIS sendiri.

## 4. TRAINING — jalur yang terbukti & jebakan yang sudah dibayar

- **Base**: `unsloth/Qwen3-1.7B` (bukan qwen3.5:0.8b! — fine-tune 0.8b via
  Unsloth/T4 GAGAL 6 lapis pada Jul 2026: arsitektur qwen3.5 butuh GPU bf16 /
  belum didukung; jangan buang waktu mengulang).
- Colab T4 + LoRA (ikuti `finetune_toti_qwen3.ipynb`).
- Ekspor **GGUF Q8_0**, nama file berversi: `toti-qwen-1.7b.Q8_0.gguf.v4`.
- **GGUF DILARANG masuk git** (GitHub limit 100MB; `.gitignore` sudah punya
  `*.gguf` & `*.gguf.*` — jangan dilonggarkan). Arsip resmi: HuggingFace
  `LasagnaS/toti-qwen-gguf`.
- Import: `ollama create toti-qwen-1.7b-v4 -f Modelfile.qwen3-1.7b`
  (sesuaikan path FROM). Model store host: `/usr/share/ollama/.ollama`
  (di-mount juga oleh container `toti-ollama`).

## 5. EVAL — syarat rilis (JANGAN dilonggarkan)

**Prinsip: eval harus meniru produksi persis. Verifikasi empiris, bukan asumsi.**

- Sampling ikut `config.py`: temperature **0.7**, top_p **0.8**,
  num_ctx **32768**, num_predict **768**, **thinking ON** (default Ollama).
- Rakitan pesan ikut runtime: system prompt + history (format penanda) +
  reminder SystemMessage + pertanyaan. Jalur `ChatOllama.bind_tools`, bukan
  curl mentah.
- Jalankan `eval_tool_calling.py` / `scenario_suite.py` (18 skenario × 3 run).
- **TAMBAHKAN skenario regresi untuk 4 insiden §2**, termasuk varian
  "history tercemar" (history berisi penanda menu/detail sebelumnya).
- **Lulus = semua target harness v3 tetap 5/5 DAN skenario regresi baru lolos.**
  Skor turun → jangan rilis, perbaiki dataset, ulangi.
- Bandingkan latensi apple-to-apple dengan `wired_test.py` (bukan prompt
  pendek; catatan: med 5.5s di harness lama = prompt pendek; produksi riil
  12–40s karena system+tools+history — jangan bandingkan silang).

## 6. (Opsional) Quantize Q4_K_M

Setelah v4 lulus di Q8_0: buat varian Q4_K_M **dari sumber f16** (bukan
re-quantize Q8), lalu jalankan SELURUH eval §5 lagi pada Q4. Lulus → pakai
(≈1.5–2× lebih cepat di CPU); gagal → tetap Q8. Kecepatan tidak pernah
menang melawan kebenaran.

## 7. Integrasi ke produksi (checklist akhir)

1. `ollama list` di host memuat model v4 → otomatis terlihat container.
2. `.env` root: `LLM_MODEL=toti-qwen-1.7b-v4` → `docker compose up -d --build chatbot-service`.
3. Tes empiris DI DALAM container (bukan cuma harness), termasuk history
   tercemar: `docker exec toti-chatbot python -c "...handle_message(...)"`
   — kasus: "ada menu apa saja?", "bento cookies kayak gimana?",
   "beli 4 cupcake", "laporan keuangan" (owner & non-owner).
4. Tes live WhatsApp minimal 1 alur penuh: menu → detail (+foto) → order →
   VA/QRIS.
5. Commit TANPA file GGUF; push; catat hasil eval di `results_*.v4.json`.

## 8. Konteks repo yang relevan

- Kode chatbot: `chatbot-service/` (prompt: `app/llm/prompt.py`,
  perakitan pesan + reminder + penanda history: `app/llm/agent.py`,
  resolver ambiguitas: `app/tools/formatting.py`).
- Stack: `docker compose up -d` (chatbot + backend Nicholas + ollama + wwebjs).
- Test suite chatbot: `cd chatbot-service && .venv/bin/python -m pytest tests/`
  (30 test — harus tetap hijau; tidak tergantung model).
