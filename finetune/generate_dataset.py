#!/usr/bin/env python3
"""Generate the Toti Cakery tool-calling fine-tuning dataset (train/validation/test).

Implements the approved dataset spec: bilingual (ID/EN) synthetic conversations
mixing tool-call examples (single-pass: rows END at the assistant tool_calls
turn) with conversational/refusal/clarification examples, using the REAL shop
menu. Deterministic (seed 42): re-running produces byte-identical files.

v4 (PROMPT_FINETUNE_V4.md §3 — pelajaran live WhatsApp 15-16 Jul 2026):
- History assistant turns pass through the runtime `_history_view()` (markers
  for menu/detail, truncation) — training context mirrors serving exactly.
- TOOL_REMINDER is appended to the system content ("\n\n"-joined), exactly how
  Ollama collates the runtime's second SystemMessage into the top system block.
- Tool arguments are the customer's words VERBATIM (no canonicalization) —
  the code-side resolver decides/asks; includes generic orders ("cupcake").
- More product-detail phrasings; N5 reduced to truly-unforwardable cases.

Run from repo root:  python finetune/generate_dataset.py
Outputs: finetune/data/{train,validation,test}.jsonl + stats.json
"""

import json
import math
import random
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chatbot-service"))

from langchain_core.utils.function_calling import convert_to_openai_tool  # noqa: E402

from app.llm.agent import OUT_OF_SCOPE_REPLY, _history_view  # noqa: E402
from app.llm.prompt import SYSTEM_PROMPT, TOOL_REMINDER  # noqa: E402
from app.tools.add_to_cart import cart_summary  # noqa: E402
from app.tools.formatting import rupiah  # noqa: E402
from app.tools.registry import ALL_TOOLS  # noqa: E402

# ── Ground-truth constants (train-serve parity) ───────────────────────────────
FAQ_HEADER = "\n\nKONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\n"
DOC_SEP = "\n\n---\n\n"
# Guard against drift: these literals must still exist in the runtime code.
_agent_src = (ROOT / "chatbot-service/app/llm/agent.py").read_text()
assert "KONTEKS FAQ (jawab pertanyaan umum berdasarkan ini):\\n" in _agent_src
# v4: runtime must still send the reminder as a SystemMessage (Ollama collates
# every system message into the TOP system block joined by "\n\n" — verified in
# ollama v0.23.2 template/template.go collate(). The dataset reproduces that
# final rendering: system content ends with "\n\n" + TOOL_REMINDER).
assert "SystemMessage(content=TOOL_REMINDER)" in _agent_src
_store_src = (ROOT / "chatbot-service/app/rag/store.py").read_text()
assert '"\\n\\n---\\n\\n".join' in _store_src

TOOLS = [convert_to_openai_tool(t) for t in ALL_TOOLS]
TOOL_NAMES = [t["function"]["name"] for t in TOOLS]

OUT_DIR = ROOT / "finetune" / "data"
PRICE_RE = re.compile(r"Rp\s?\d|\d\.\d{3}|stok\s+(ada|habis)", re.I)

# ── Menu (REAL Toti Cakery menu) ──────────────────────────────────────────────
# canonical -> (category, flavours)
CUP_FLAV = ["Cokelat", "Vanilla"]
COOKIE_FLAV = ["Original", "Brown Sugar", "Red Velvet", "Matcha"]
MENU = {
    "Cupcakes isi 4": ("cupcake", CUP_FLAV),
    "Cupcakes isi 6": ("cupcake", CUP_FLAV),
    "Cupcakes isi 9": ("cupcake", CUP_FLAV),
    "Cupcakes Tart isi 7": ("cupcake", CUP_FLAV),
    "Cake 10cm": ("cake", CUP_FLAV),
    "Cake 15cm": ("cake", CUP_FLAV),
    "Cake 18cm": ("cake", CUP_FLAV),
    "Cake 20cm": ("cake", CUP_FLAV),
    "Cake 22cm": ("cake", CUP_FLAV),
    "Mini Cookies 7cm": ("cookies", COOKIE_FLAV),
    "Bento Cookies 10cm": ("cookies", COOKIE_FLAV),
    "Giant Cookies 15cm": ("cookies", COOKIE_FLAV),
}
HOLDOUT_PRODUCTS = {"Cake 22cm", "Giant Cookies 15cm"}  # test-only
HOLDOUT_FLAVOUR = "Matcha"                               # test-only

BASE_SURFACES = {
    "Cupcakes isi 4": ["cupcakes isi 4", "cup cake isi 4", "cupcake yang isi 4"],
    "Cupcakes isi 6": ["cupcakes isi 6", "cupcake isi 6", "cupcakes yang isi 6"],
    "Cupcakes isi 9": ["cupcakes isi 9", "cupcake isi 9", "cupcakes yang isi 9"],
    "Cupcakes Tart isi 7": ["cupcakes tart", "cupcake tart isi 7", "cupcakes tart yang isi 7"],
    "Cake 10cm": ["cake 10cm", "cake 10 cm", "cake ukuran 10cm", "kue tart 10cm"],
    "Cake 15cm": ["cake 15cm", "cake 15 cm", "cake ukuran 15cm", "kue tart 15cm"],
    "Cake 18cm": ["cake 18cm", "cake 18 cm", "cake ukuran 18cm", "kue tart 18cm"],
    "Cake 20cm": ["cake 20cm", "cake 20 cm", "cake ukuran 20cm", "kue tart 20cm"],
    "Cake 22cm": ["cake 22cm", "cake 22 cm", "cake ukuran 22cm", "kue tart 22cm"],
    "Mini Cookies 7cm": ["mini cookies", "mini cookies 7cm", "cookies mini"],
    "Bento Cookies 10cm": ["bento cookies", "cookies bento", "bento cookies 10cm"],
    "Giant Cookies 15cm": ["giant cookies", "cookies giant", "giant cookies 15cm"],
}
FLAV_SURFACE_ID = {"Cokelat": ["coklat", "cokelat", "rasa coklat"],
                   "Vanilla": ["vanilla", "vanila", "rasa vanilla"],
                   "Original": ["original", "yang original"],
                   "Brown Sugar": ["brown sugar", "rasa brown sugar"],
                   "Red Velvet": ["red velvet", "rasa red velvet"],
                   "Matcha": ["matcha", "rasa matcha"]}
FLAV_SURFACE_EN = {"Cokelat": ["chocolate"], "Vanilla": ["vanilla"],
                   "Original": ["original"], "Brown Sugar": ["brown sugar"],
                   "Red Velvet": ["red velvet"], "Matcha": ["matcha"]}

CATEGORY_SURFACES_ID = [("cupcakes", "cupcake"), ("cup cake", "cupcake"),
                        ("kue tart", "cake"), ("kue ultah", "cake"), ("cake", "cake"),
                        ("cookies", "cookies"), ("kukis", "cookies"), ("kue kering", "cookies")]
CATEGORY_SURFACES_EN = [("cupcakes", "cupcake"), ("birthday cakes", "cake"),
                        ("cakes", "cake"), ("cookies", "cookies")]

# ── Lexicons ──────────────────────────────────────────────────────────────────
QTY_ID = [("1", 1), ("satu", 1), ("2", 2), ("dua", 2), ("3", 3), ("tiga", 3),
          ("4", 4), ("empat", 4), ("5", 5), ("lima", 5), ("6", 6), ("7", 7),
          ("8", 8), ("10", 10), ("sepuluh", 10), ("sepasang", 2),
          ("selusin", 12), ("setengah lusin", 6), ("2 lusin", 24)]
QTY_EN = [("one", 1), ("two", 2), ("three", 3), ("four", 4), ("five", 5),
          ("2", 2), ("3", 3), ("a dozen", 12), ("half a dozen", 6), ("a couple of", 2)]
UNITS_ID = ["", "", "", " pcs", " biji", " buah", " kotak", " box"]
GREET_ID = ["", "", "", "halo, ", "hai kak, ", "permisi, ", "kak, ", "min, ", "selamat siang, "]
GREET_EN = ["", "", "hi, ", "hello, ", "excuse me, "]
PART_ID = ["", "", "", " dong", " ya", " kak", " nih", " deh", " min"]
PART_EN = ["", "", " please", ""]
EMOJI = ["", "", " 😊", " 🙏", " 😄", " 🎂"]

ABBREV = [("yang", "yg"), ("tidak", "ga"), ("berapa", "brp"), ("gimana", "gmn"),
          ("sudah", "udah"), ("banget", "bgt"), ("sama", "sm"), ("boleh", "blh")]


def _typo_ops(rng: random.Random, text: str) -> str:
    ops = rng.sample(["abbrev", "vowel", "double", "swap", "lower", "qq"], k=rng.choice([1, 2]))
    for op in ops:
        if op == "abbrev":
            for a, b in rng.sample(ABBREV, k=len(ABBREV)):
                if re.search(rf"\b{a}\b", text):
                    text = re.sub(rf"\b{a}\b", b, text, count=1)
                    break
        elif op in ("vowel", "double", "swap"):
            words = text.split(" ")
            idxs = [i for i, w in enumerate(words) if len(w) > 4 and w.isalpha()]
            if not idxs:
                continue
            i = rng.choice(idxs)
            w = words[i]
            if op == "vowel":
                vowels = [j for j, ch in enumerate(w) if ch in "aiueo"]
                if len(vowels) > 1:
                    j = rng.choice(vowels)
                    w = w[:j] + w[j + 1:]
            elif op == "double":
                j = rng.randrange(1, len(w))
                w = w[:j] + w[j] + w[j:]
            else:
                j = rng.randrange(len(w) - 1)
                w = w[:j] + w[j + 1] + w[j] + w[j + 2:]
            words[i] = w
            text = " ".join(words)
        elif op == "lower":
            text = text.lower()
        elif op == "qq" and text.endswith("?"):
            text = text + "?"
    return text


def render(tpl: str, **slots) -> str:
    s = tpl.format(**slots)
    s = re.sub(r"[ \t]+", " ", s).strip()
    s = re.sub(r" ([?,.!])", r"\1", s)
    return s


# ── FAQ documents (5 real + 7 synthetic, same Q/A style) ─────────────────────
def _read_faq(n: int) -> str:
    return (ROOT / f"chatbot-service/knowledge_base/faq/faq{n}.txt").read_text().strip()


FAQ_DOCS = [
    {"id": "faq1", "doc": _read_faq(1),
     "q_id": ["jam berapa toti cakery buka{part}?", "buka sampai jam berapa{part}?",
              "hari minggu buka ga{part}?", "jam operasionalnya gimana{part}?"],
     "q_en": ["what are your opening hours?", "are you open on Sundays?"],
     "a_id": ["Toti Cakery buka Senin sampai Sabtu pukul 09.00 - 19.00 WIB ya kak, hari Minggu kami libur 😊",
              "Kami buka setiap Senin-Sabtu jam 09.00 sampai 19.00 WIB. Hari Minggu libur ya kak 🙏"],
     "a_en": ["We're open Monday to Saturday, 09.00 - 19.00 WIB. We're closed on Sundays 😊"]},
    {"id": "faq2", "doc": _read_faq(2),
     "q_id": ["terima pesanan kue custom ga{part}?", "bisa pesan kue ulang tahun desain khusus{part}?",
              "kalau mau kue custom gimana{part}?", "bisa request tema kue ga{part}?"],
     "q_en": ["do you take custom cake orders?", "can I order a custom birthday cake?"],
     "a_id": ["Bisa kak! Kami menerima kue custom seperti kue ulang tahun dengan desain, tulisan, dan tema tertentu. Karena butuh diskusi detail, pesanan custom akan diteruskan ke admin kami ya 😊",
              "Ya, kue custom bisa banget. Nanti kebutuhanmu kami teruskan ke admin karena perlu diskusi detail desain dan temanya ya kak 🙏"],
     "a_en": ["Yes! We take custom cakes (special designs, writing, themes). Custom orders are forwarded to our admin for the detailed discussion 😊"]},
    {"id": "faq3", "doc": _read_faq(3),
     "q_id": ["pengirimannya gimana{part}?", "bisa dikirim ke rumah ga{part}?",
              "ambil sendiri bisa{part}?", "delivery pakai apa{part}?"],
     "q_en": ["how does delivery work?", "can I pick up my order at the store?"],
     "a_id": ["Ada dua cara kak: ambil sendiri (pickup) di toko, atau delivery. Untuk delivery, kurirnya dipesan sendiri lewat ojek online seperti GoSend/GrabExpress — saat pesanan siap kami kirimkan nama dan alamat toko biar gampang pesan kurirnya 😊",
              "Bisa pickup di toko atau delivery ya. Kalau delivery, pelanggan memesan sendiri kurir ojek online (GoSend/GrabExpress) dari alamat toko kami 🙏"],
     "a_en": ["Two options: pickup at the store, or delivery — you book your own courier (GoSend/GrabExpress) from our store address. We'll send the store name and full address once your order is ready 😊"]},
    {"id": "faq4", "doc": _read_faq(4),
     "q_id": ["bayarnya bisa pakai apa aja{part}?", "bisa bayar pakai QRIS{part}?",
              "pembayarannya gimana{part}?", "bisa DP dulu ga{part}?"],
     "q_en": ["what payment methods do you accept?", "can I pay a down payment first?"],
     "a_id": ["Pembayaran non-tunai ya kak, lewat QRIS atau transfer Virtual Account (VA) bank. Setelah konfirmasi pesanan kami kirim QR/nomor VA-nya. Bisa bayar penuh atau DP 50% 😊",
              "Bisa QRIS atau Virtual Account bank kak. Tersedia opsi bayar penuh atau DP 50%, dan pesanan diproses setelah pembayaran kami terima ya 🙏"],
     "a_en": ["We accept cashless payment via QRIS or bank Virtual Account (VA). You can pay in full or a 50% down payment; the order is processed once payment is received 😊"]},
    {"id": "faq5", "doc": _read_faq(5),
     "q_id": ["kuenya halal ga{part}?", "kue tahan berapa lama{part}?",
              "cara simpan kuenya gimana{part}?", "aman ga disimpan sampai besok{part}?"],
     "q_en": ["are your cakes halal?", "how long do the cakes last?"],
     "a_id": ["Semua produk kami dibuat dari bahan halal kak. Paling enak dinikmati di hari yang sama; kalau disimpan di chiller tahan sekitar 2-3 hari. Hindari suhu ruang terlalu lama ya, apalagi yang berkrim segar 😊",
              "Halal ya kak 😊 Untuk daya tahan, kue terbaik dimakan di hari yang sama, atau simpan di lemari pendingin supaya tahan sekitar 2-3 hari."],
     "a_en": ["All our products are made with halal ingredients. Best enjoyed the same day; they keep about 2-3 days in the chiller. Avoid leaving them at room temperature too long 😊"]},
    {"id": "faq_s1",
     "doc": ("Q: Apakah bisa pesan untuk diambil di hari yang sama?\n"
             "A: Pesanan reguler bisa diproses di hari yang sama selama slot produksi masih tersedia. "
             "Untuk kue custom dan pesanan dalam jumlah besar, pemesanan minimal H-2 agar hasilnya maksimal."),
     "q_id": ["bisa pesan buat hari ini juga ga{part}?", "kalau pesan sekarang bisa jadi hari ini{part}?",
              "pesan dadakan bisa{part}?"],
     "q_en": ["can I order for same-day pickup?", "is same-day order possible?"],
     "a_id": ["Pesanan reguler bisa diproses di hari yang sama selama slot produksi masih ada kak. Kalau kue custom atau jumlah besar, minimal H-2 ya 😊"],
     "a_en": ["Regular orders can be same-day while production slots last; custom cakes and big orders need at least 2 days ahead (H-2) 😊"]},
    {"id": "faq_s2",
     "doc": ("Q: Toti Cakery lokasinya di mana dan melayani area mana saja?\n"
             "A: Toko kami berlokasi di Batam. Untuk pengiriman, jangkauannya mengikuti layanan ojek online "
             "(GoSend/GrabExpress) dari alamat toko, jadi selama area kamu terjangkau layanan tersebut pesanan bisa dikirim."),
     "q_id": ["tokonya di mana{part}?", "melayani area mana aja{part}?", "aku di batam centre kejangkau ga{part}?"],
     "q_en": ["where is your store located?", "which areas do you deliver to?"],
     "a_id": ["Toko kami di Batam kak. Jangkauan kirim mengikuti layanan ojek online (GoSend/GrabExpress) dari alamat toko — selama areamu terjangkau layanan itu, aman 😊"],
     "a_en": ["We're located in Batam. Delivery coverage follows the online courier services (GoSend/GrabExpress) from our store address 😊"]},
    {"id": "faq_s3",
     "doc": ("Q: Apakah produk mengandung alergen?\n"
             "A: Produk kami umumnya mengandung gluten (terigu), telur, dan susu. Beberapa varian dapat mengandung "
             "atau terpapar kacang-kacangan. Jika kamu punya alergi, mohon informasikan saat memesan."),
     "q_id": ["kuenya mengandung kacang ga{part}?", "aku alergi susu, aman ga{part}?", "ada info alergen ga{part}?"],
     "q_en": ["do your products contain nuts?", "I have a milk allergy, is that okay?"],
     "a_id": ["Produk kami umumnya mengandung gluten, telur, dan susu kak, dan beberapa varian bisa terpapar kacang. Kalau ada alergi, kabari saat memesan ya 🙏"],
     "a_en": ["Our products generally contain gluten, eggs, and milk, and some variants may contain or contact nuts. Please tell us about any allergy when ordering 🙏"]},
    {"id": "faq_s4",
     "doc": ("Q: Apakah kue dikemas dengan box dan bisa menambahkan kartu ucapan?\n"
             "A: Setiap kue dikemas dengan box yang aman untuk dibawa. Kartu ucapan gratis — tuliskan pesanmu saat "
             "memesan. Lilin dan topper sederhana juga tersedia atas permintaan."),
     "q_id": ["dapat box ga kuenya{part}?", "bisa tambah kartu ucapan{part}?", "ada lilinnya ga{part}?"],
     "q_en": ["can I add a greeting card to the cake?", "does the cake come in a box?"],
     "a_id": ["Setiap kue dikemas box yang aman kak. Kartu ucapan gratis — tulis saja pesanmu saat memesan; lilin dan topper sederhana juga bisa diminta 😊"],
     "a_en": ["Every cake comes in a safe box. Greeting cards are free — just write your message when ordering; candles and simple toppers are available on request 😊"]},
    {"id": "faq_s5",
     "doc": ("Q: Bagaimana kebijakan pembatalan dan refund DP?\n"
             "A: Pesanan yang belum dibayar bisa dibatalkan kapan saja lewat chat. Pesanan yang sudah dibayar "
             "(penuh maupun DP) tidak dapat dibatalkan otomatis — silakan hubungi admin untuk penanganan lebih lanjut."),
     "q_id": ["kalau batal DP nya balik ga{part}?", "kebijakan pembatalannya gimana{part}?",
              "pesanan yang udah dibayar bisa dibatalkan{part}?"],
     "q_en": ["what's your cancellation policy?", "is the down payment refundable?"],
     "a_id": ["Pesanan yang belum dibayar bisa dibatalkan kapan saja lewat chat kak. Kalau sudah dibayar (penuh/DP), pembatalan tidak bisa otomatis — nanti dibantu admin ya 🙏"],
     "a_en": ["Unpaid orders can be cancelled anytime via chat. Paid orders (full or DP) can't be cancelled automatically — our admin will handle it 🙏"]},
    {"id": "faq_s6",  # TEST-ONLY
     "doc": ("Q: Apakah bisa memesan lewat website Toti Cakery?\n"
             "A: Bisa. Selain lewat chat ini, kamu dapat memesan melalui website Toti Cakery. Perlu diingat, setiap "
             "pelanggan hanya dapat memiliki satu pesanan aktif pada satu waktu."),
     "q_id": ["bisa pesan lewat website ga{part}?", "selain chat, order bisa dari mana{part}?"],
     "q_en": ["can I order from your website instead?", "is there another way to order besides chat?"],
     "a_id": ["Bisa kak — selain lewat chat ini, kamu dapat memesan melalui website Toti Cakery. Perlu diingat, setiap pelanggan hanya dapat memiliki satu pesanan aktif pada satu waktu ya 😊"],
     "a_en": ["Yes — besides this chat you can order via the Toti Cakery website. Note: each customer can only have one active order at a time 😊"]},
    {"id": "faq_s7",  # TEST-ONLY
     "doc": ("Q: Apakah ada promo atau diskon?\n"
             "A: Saat ini tidak ada program diskon tetap. Promo sesekali diumumkan melalui Instagram Toti Cakery, "
             "dan harga yang tercantum pada menu adalah harga final."),
     "q_id": ["ada promo ga sekarang{part}?", "diskon dong kak{part}", "ada potongan harga ga{part}?"],
     "q_en": ["do you have any discounts right now?", "any ongoing promo?"],
     "a_id": ["Saat ini belum ada program diskon tetap kak 🙏 Promo sesekali diumumkan lewat Instagram Toti Cakery, dan harga pada menu adalah harga final ya."],
     "a_en": ["We don't have a standing discount program right now — occasional promos are announced on our Instagram, and menu prices are final 🙏"]},
]
TEST_ONLY_DOCS = {"faq_s6", "faq_s7"}

ID_STOPWORDS = set("yang di ke dari dan atau untuk pada dengan kami kamu kak ya adalah bisa dapat juga saat lewat "
                   "itu ini nya akan sudah belum tidak ga hanya per satu dua tiga saja silakan mohon setiap agar "
                   "supaya kalau jika hari jam a q kami. adalah".split())


def _grounded(answer: str, doc: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9.\-]+", answer.lower()) if w not in ID_STOPWORDS and len(w) > 2]
    if not words:
        return False
    dl = doc.lower()
    hits = sum(1 for w in words if w in dl)
    return hits / len(words) >= 0.6


# ── Template pools per type ───────────────────────────────────────────────────
T1_ID = ["{greet}menu nya apa aja{part}?", "{greet}ada kue apa aja di toti cakery{part}?",
         "{greet}mau liat menu{part}", "{greet}list menu sama harganya{part}",
         "{greet}jual apa aja sih{part}?", "{greet}harga kue-kuenya berapaan{part}?",
         "{greet}boleh minta daftar menunya{part}?", "{greet}kue yang tersedia hari ini apa aja{part}?",
         "{greet}pengen liat pilihan kuenya{part}", "{greet}masih ada stok kue ga{part}?",
         "{greet}{prod} masih ada{part}?", "{greet}masih tersedia ga {prod} nya{part}?",
         "{greet}stok {prod} ready{part}?", "{greet}ready stock apa aja hari ini{part}?",
         "{greet}katalognya{part}", "{greet}apa aja yang dijual di sini{part}?"]
T1_EN = ["{greet}what's on the menu{part}?", "{greet}what cakes do you have?",
         "{greet}can I see the menu{part}?", "{greet}what do you sell here?",
         "{greet}how much are your cakes in general?", "{greet}is the {prod} still available?",
         "{greet}what's available today?", "{greet}send me your price list{part}"]

T2_ID = ["{greet}ada {cat} apa aja{part}?", "{greet}menu {cat} nya{part}",
         "{greet}liat pilihan {cat} nya{part}", "{greet}yang kategori {cat} ada apa aja{part}?",
         "{greet}jual {cat} ga{part}?", "{greet}rekomendasi {cat}{part}",
         "{greet}pengen {cat}, pilihannya apa aja{part}?", "{greet}daftar {cat} sama harganya{part}?",
         "{greet}{cat} nya ada varian apa aja{part}?", "{greet}mau liat menu khusus {cat}{part}"]
T2_EN = ["{greet}what {cat} do you have?", "{greet}show me your {cat} options{part}",
         "{greet}any {cat} on the menu?", "{greet}I want to see the {cat} list",
         "{greet}do you sell {cat}?"]

# v4: varian frasa detail diperbanyak (insiden live #2 — "bento cookies kayak
# gimana ya?" salah routing ke get_menu). Template baru di DEPAN pool supaya
# masuk regime main (train/val); slice test pool tetap di ekor.
T3_ID = ["{greet}{prod} kayak gimana ya{part}?", "{greet}{prod} seperti apa{part}?",
         "{greet}{prod} kaya apa sih{part}?", "{greet}bentuknya {prod} gimana{part}?",
         "{greet}ada fotonya {prod} ga{part}?", "{greet}ceritain dong {prod}{part}",
         "{greet}{prod} tuh yang kayak gimana{part}?",
         "{greet}{prod} itu kayak gimana{part}?", "{greet}{prod} itu apa sih{part}?",
         "{greet}boleh liat foto {prod}{part}?", "{greet}deskripsi {prod}{part}",
         "{greet}{prod} rasanya kayak apa{part}?", "{greet}cerita tentang {prod}{part}",
         "{greet}{prod} cocok buat ulang tahun ga{part}?", "{greet}detail {prod}{part}",
         "{greet}penasaran sama {prod}, kayak gimana{part}?", "{greet}{prod} ukurannya segimana{part}?",
         "{greet}{prod} itu buat berapa orang{part}?", "{greet}liat penampakan {prod}{part}",
         "{greet}{prod} manis banget ga{part}?", "{greet}spill {prod}{part}",
         "{greet}info {prod}{part}?", "{greet}{prod} pakai topping apa{part}?"]
T3_EN = ["{greet}what does the {prod} look like?", "{greet}got a picture of the {prod}?",
         "{greet}what's the {prod} like exactly?",
         "{greet}what is the {prod} like?", "{greet}can I see a photo of the {prod}?",
         "{greet}tell me about the {prod}", "{greet}what does the {prod} taste like?",
         "{greet}how big is the {prod}?", "{greet}is the {prod} good for birthdays?",
         "{greet}details on the {prod}{part}", "{greet}what's on the {prod}?"]

T4_ID = ["{greet}bagusan mana {prodA} sama {prodB}{part}?", "{greet}bedanya {prodA} dan {prodB} apa{part}?",
         "{greet}bandingin {prodA} sama {prodB}{part}", "{greet}enakan {prodA} atau {prodB}{part}?",
         "{greet}worth it mana {prodA} sama {prodB}{part}?", "{greet}{prodA} vs {prodB}, pilih mana{part}?",
         "{greet}buat ultah mending {prodA} atau {prodB}{part}?",
         "{greet}bingung antara {prodA}, {prodB}, sama {prodC}, bedanya apa{part}?",
         "{greet}gedean mana {prodA} sama {prodB}{part}?", "{greet}rekomen mana {prodA} apa {prodB}{part}?"]
T4_EN = ["{greet}which is better, the {prodA} or the {prodB}?",
         "{greet}what's the difference between the {prodA} and the {prodB}?",
         "{greet}compare the {prodA} and the {prodB}{part}",
         "{greet}should I get the {prodA} or the {prodB}?",
         "{greet}{prodA} vs {prodB}, which do you recommend?"]

# v4 (§3.3, insiden live #4): surface generik utk order ambigu — model WAJIB
# meneruskan kata pelanggan apa adanya ("cupcake", bukan mengarang "isi 6");
# resolver kode (app/tools/formatting.py) yang menanyakan pilihan. Semua entri
# di sini token-match >=1 produk nyata sehingga resolver menjawab opsi, bukan
# "tidak ditemukan".
GENERIC_SURFACES = ["cupcake", "cupcakes", "cake", "cookies"]

T5_ID = ["{greet}mau pesan {prod} {qty}{unit}{part}", "{greet}aku mau {prod} {qty}{unit} ya{part}",
         "{greet}pesan {prod} {qty}{unit}{part}", "{greet}order {prod} {qty}{unit}{part}",
         "{greet}beli {prod} {qty}{unit} ya{part}", "{greet}mau ambil {prod} {qty}{unit}{part}",
         "{greet}bisa pesan {prod} {qty}{unit}{part}?", "{greet}aku pengen {prod}, {qty}{unit} ya{part}",
         "{greet}tolong siapin {prod} {qty}{unit}{part}", "{greet}checkout {prod} {qty}{unit}{part}",
         "{greet}mau order {prod} {qty}{unit}, bisa{part}?", "{greet}{prod} nya {qty}{unit} ya{part}",
         "{greet}ambil {prod} {qty}{unit} deh{part}", "{greet}gas {prod} {qty}{unit}{part}",
         "{greet}mau pesan {prod} ya{part}", "{greet}aku mau beli {prod}{part}"]
T5_ID_NOQTY = {14, 15}  # template indexes where qty defaults to 1
T5_EN = ["{greet}I'd like to order {qty} {prod}", "{greet}can I get {qty} {prod}?",
         "{greet}I want to buy {qty} {prod}", "{greet}please prepare {qty} {prod}",
         "{greet}I'll take {qty} {prod}", "{greet}I want the {prod}, {qty} please",
         "{greet}one {prod} please", "{greet}I'd like a {prod}{part}"]
T5_EN_NOQTY = {6, 7}

T6_ID = ["{greet}mau pesan {items}{part}", "{greet}order: {items}{part}",
         "{greet}aku ambil {items} ya{part}", "{greet}beli {items}{part}",
         "{greet}pesan ya: {items}{part}", "{greet}mau {items}, bisa{part}?",
         "{greet}checkout {items}{part}", "{greet}buat acara, mau {items}{part}",
         "{greet}tolong {items} ya{part}", "{greet}gas {items}{part}"]
T6_EN = ["{greet}can I order {items}?", "{greet}I'd like {items}{part}",
         "{greet}I want to get {items}", "{greet}please add {items}",
         "{greet}order for me: {items}"]

# v4: varian T7 "menu" ("yang kedua") DIHAPUS — daftar menu di history kini
# penanda ringkas, urutan item tak terlihat lagi; kasusnya pindah ke N5
# deictic_menu (tanya balik). Varian "detail" tetap: nama produk masih terbaca
# di penanda detail, jadi follow-up bisa diteruskan ke add_to_cart.
T7_DETAIL_ID = ["oke gas, {qty}{unit} ya{part}", "boleh, ambil {qty}{unit}{part}",
                "yaudah pesan itu {qty}{unit}{part}", "oke mau yang itu {qty}{unit}{part}",
                "sip, pesan {qty}{unit} ya{part}"]
T7_ADD_ID = ["eh tambah {prod} {qty}{unit}{part}", "tambahin {prod} {qty}{unit} ya{part}",
             "sekalian {prod} {qty}{unit}{part}"]
T7_DETAIL_EN = ["okay I'll take {qty}", "let's go with that, {qty} please", "sounds good, {qty} of those"]
T7_ADD_EN = ["also add {qty} {prod}", "add {qty} {prod} as well", "and {qty} {prod} too please"]

T8_ID = ["{greet}pesananku udah sampai mana{part}?", "{greet}orderanku gimana statusnya{part}?",
         "{greet}kue ku udah jadi belum{part}?", "{greet}status pesanan{part}?",
         "{greet}pembayaranku udah masuk belum{part}?", "{greet}udah diproses belum ya pesananku{part}?",
         "{greet}kapan pesananku siap{part}?", "{greet}cek order ku{part}",
         "{greet}pesanan atas namaku gimana{part}?", "{greet}invoiceku statusnya apa{part}?",
         "{greet}udah dibikin belum kuenya{part}?", "{greet}progress pesananku{part}?",
         "{greet}orderku kemarin gimana{part}?", "{greet}udah bisa diambil belum pesananku{part}?",
         "{greet}pesananku jadi dikirim kapan{part}?", "{greet}gimana kabar orderanku{part}?"]
T8_EN = ["{greet}where's my order?", "{greet}what's the status of my order?",
         "{greet}is my cake ready yet?", "{greet}has my payment gone through?",
         "{greet}when will my order be ready?", "{greet}any update on my order?",
         "{greet}is my order being processed?", "{greet}can I pick up my order now?"]

T9_ID = ["{greet}batalin pesananku{part}", "{greet}aku mau cancel order{part}",
         "{greet}gajadi deh pesanannya, batalin ya{part}", "{greet}tolong batalkan pesananku{part}",
         "{greet}cancel pesananku yang tadi{part}", "{greet}ga jadi beli, batalin aja{part}",
         "{greet}mau membatalkan pesanan{part}", "{greet}batal aja deh ordernya{part}",
         "{greet}cancel order ku ya{part}", "{greet}urungkan pesananku{part}"]
T9_EN = ["{greet}please cancel my order", "{greet}I want to cancel my order",
         "{greet}cancel it please, I changed my mind", "{greet}can you cancel my order?",
         "{greet}never mind, cancel the order"]

CUSTOM_THEMES = ["dinosaurus", "princess", "mobil balap", "bola", "unicorn", "superhero",
                 "bunga vintage", "karakter kartun", "luar angkasa", "kucing lucu"]
COMPLAINTS = ["penyok saat sampai", "datang terlambat", "rusak kemasannya", "kurang sesuai pesanan"]
T10_ID = [("{greet}bisa buat kue ultah custom tema {theme}{part}?",
           "Pelanggan ingin kue ulang tahun custom tema {theme}"),
          ("{greet}mau pesan wedding cake {tiers} tingkat bisa{part}?",
           "Pelanggan menanyakan wedding cake {tiers} tingkat"),
          ("{greet}bisa tulis ucapan khusus di atas kuenya{part}?",
           "Pelanggan minta tulisan ucapan khusus di atas kue"),
          ("{greet}kue yang kemarin {complaint}, gimana nih{part}?",
           "Pelanggan komplain kue pesanannya {complaint}"),
          ("{greet}aku mau ngomong sama admin langsung{part}",
           "Pelanggan ingin berbicara langsung dengan admin"),
          ("{greet}bisa custom bentuk {theme}{part}?",
           "Pelanggan ingin kue custom bentuk {theme}"),
          ("{greet}ada yang bisa bantu desain kue buat anniversary{part}?",
           "Pelanggan butuh bantuan desain kue anniversary"),
          ("{greet}bisa request dekorasi warna tertentu satu set{part}?",
           "Pelanggan request dekorasi kue dengan tema warna khusus"),
          ("{greet}pesananku kayaknya salah kirim, tolong{part}",
           "Pelanggan komplain pesanan diduga salah kirim"),
          ("{greet}mau nego harga buat order kantor jumlah besar, bisa{part}?",
           "Pelanggan ingin nego harga pesanan kantor jumlah besar")]
T10_EN = [("{greet}can you make a custom birthday cake with a dinosaur theme?",
           "Pelanggan ingin kue ulang tahun custom tema dinosaurus"),
          ("{greet}I need to speak to a human please",
           "Pelanggan ingin berbicara langsung dengan admin"),
          ("{greet}my cake arrived damaged yesterday, what now?",
           "Pelanggan komplain kue pesanannya rusak saat tiba"),
          ("{greet}can I order a {tiers_en}-tier wedding cake?",
           "Pelanggan menanyakan wedding cake {tiers} tingkat"),
          ("{greet}can you write a custom message on the cake?",
           "Pelanggan minta tulisan ucapan khusus di atas kue")]

T11_ID = ["{greet}laporan keuangan bulan ini{part}", "{greet}omzet bulan ini berapa{part}?",
          "{greet}pendapatan toti cakery bulan ini gimana{part}?", "{greet}laba bulan ini berapa{part}?",
          "{greet}rekap keuangan{part}", "{greet}berapa pemasukan kita bulan ini{part}?",
          "{greet}keuangan toko gimana bulan ini{part}?", "{greet}emang untung berapa sih sebulan{part}?"]
T11_EN = ["{greet}show me this month's financial report", "{greet}what's our revenue this month?"]
T12_ID = ["{greet}produk terlaris apa bulan ini{part}?", "{greet}analitik penjualan{part}",
          "{greet}kue apa yang paling laku{part}?", "{greet}rata-rata nilai order berapa{part}?",
          "{greet}statistik bisnis bulan ini{part}", "{greet}best seller kita apa{part}?",
          "{greet}penjualan paling banyak produk mana{part}?", "{greet}analisa penjualan dong min{part}"]
T12_EN = ["{greet}what's our best seller this month?", "{greet}show me the sales analytics"]

N2_ID = [("{greet}ada cabang di {city} ga{part}?", "branch"),
         ("{greet}bisa COD ga{part}?", "cod"),
         ("{greet}bisa kirim ke luar kota{part}?", "outcity"),
         ("{greet}ada program franchise ga{part}?", "franchise"),
         ("{greet}lagi buka lowongan kerja ga{part}?", "job"),
         ("{greet}bisa sewa tempat buat acara di toko{part}?", "venue"),
         ("{greet}kue nya bisa dikirim pakai kurir kargo ke {city}{part}?", "outcity"),
         ("{greet}bisa jadi reseller kue kalian ga{part}?", "reseller")]
N2_EN = [("{greet}do you have a branch in {city}?", "branch"),
         ("{greet}can I pay cash on delivery?", "cod"),
         ("{greet}do you ship to other cities?", "outcity"),
         ("{greet}are you hiring right now?", "job")]
CITIES = ["Jakarta", "Bandung", "Surabaya", "Medan", "Pekanbaru"]
N2_REPLY_ID = ["Waduh, aku belum punya info soal itu 🙏 Mau aku sambungkan ke admin biar dibantu langsung?",
               "Maaf kak, untuk hal itu aku belum punya informasinya. Mau kuteruskan ke admin supaya dijawab langsung?",
               "Hmm, aku belum bisa jawab soal itu 🙏 Kalau mau, aku bisa hubungkan kamu ke admin ya.",
               "Info soal itu belum aku pegang kak. Mau dibantu admin langsung?"]
N2_REPLY_EN = ["I don't have that information yet, sorry 🙏 Want me to connect you with our admin?",
               "I'm not sure about that one — shall I forward you to our admin so they can help directly?"]

N3_ID = [("halo{part}", "greet"), ("hai kak", "greet"), ("assalamualaikum", "greet"),
         ("permisi{part}", "greet"), ("selamat pagi{part}", "greet"), ("malem kak", "greet"),
         ("halo bot", "bot"), ("kamu bot ya{part}?", "bot"), ("kamu manusia atau robot{part}?", "bot"),
         ("makasih ya kak!", "thanks"), ("makasih banyak infonya{part}", "thanks"),
         ("oke sip{part}", "ack"), ("mantap{part}", "ack"), ("oke deh, nanti aku kabari lagi", "ack"),
         ("sip, jelas banget", "thanks"), ("wih keren juga ya toko ini", "ack")]
N3_EN = [("hi!", "greet"), ("hello{part}", "greet"), ("good morning!", "greet"),
         ("thanks a lot!", "thanks"), ("okay great{part}", "ack"), ("are you a bot?", "bot"),
         ("hey there", "greet"), ("thank you so much!", "thanks"), ("good evening", "greet"),
         ("thanks, that helps!", "thanks"), ("alright, noted", "ack"), ("cool, thanks!", "thanks"),
         ("am I talking to a robot?", "bot"), ("hiya", "greet"), ("many thanks!", "thanks"),
         ("okay got it, thank you", "thanks")]
N3_REPLY = {
    ("greet", "id"): ["Halo! Selamat datang di Toti Cakery 😊 Mau lihat menu atau pesan sesuatu?",
                      "Hai kak! Ada yang bisa kubantu? Bisa tanya menu, pesan kue, atau cek pesanan ya 😊",
                      "Halo, dengan asisten Toti Cakery di sini 🎂 Mau kubantu apa hari ini?",
                      "Waalaikumsalam! Selamat datang di Toti Cakery 😊 Ada yang bisa kubantu?"],
    ("bot", "id"): ["Iya, aku asisten virtual Toti Cakery 😄 Tapi tenang, aku bisa bantu kamu lihat menu sampai pesan kue. Mau coba?",
                    "Aku bot asisten Toti Cakery kak 😊 Kalau butuh admin manusia, bilang aja nanti kuteruskan ya."],
    ("thanks", "id"): ["Sama-sama kak! Kalau butuh apa-apa lagi, chat aja ya 😊",
                       "Siap, senang bisa bantu! Ditunggu pesanan berikutnya ya 🎂"],
    ("ack", "id"): ["Siap kak! Kalau sudah mau pesan atau ada pertanyaan lain, aku di sini ya 😊",
                    "Oke! Sampai nanti, semoga harimu manis kayak kue 😄"],
    ("greet", "en"): ["Hi! Welcome to Toti Cakery 😊 Would you like to see the menu or place an order?",
                      "Hello! How can I help you today — menu, orders, or anything about the shop? 😊"],
    ("bot", "en"): ["Yes, I'm Toti Cakery's virtual assistant 😄 I can help with the menu, orders, and more. Want to try?"],
    ("thanks", "en"): ["You're welcome! Message me anytime you need something 😊"],
    ("ack", "en"): ["Great! I'm here whenever you're ready to order 😊"],
}

N4_ID = ["{greet}bantuin PR matematika{part}", "{greet}menurutmu presiden sekarang gimana{part}?",
         "{greet}resep bolu yang enak gimana{part}?", "{greet}translate 'selamat pagi' ke bahasa jepang{part}",
         "{greet}cuaca hari ini gimana{part}?", "{greet}rekomendasi film{part}",
         "{greet}cara bikin website gimana{part}?", "{greet}jodohku siapa ya{part}?",
         "{greet}1+1 berapa{part}?", "{greet}ceritain sejarah indonesia{part}",
         "{greet}harga iphone terbaru berapa{part}?", "{greet}lagu yang lagi hits apa{part}?",
         "{greet}menurutmu crypto bakal naik ga{part}?", "{greet}buatin puisi cinta{part}",
         "{greet}cara diet cepat gimana{part}?", "{greet}kenapa langit warnanya biru{part}?"]
N4_EN = ["{greet}what's the capital of France?", "{greet}help me with my math homework",
         "{greet}write me a love poem{part}", "{greet}what's the weather today?",
         "{greet}who will win the next election?", "{greet}give me a cake recipe{part}",
         "{greet}what's the best phone to buy?", "{greet}tell me a joke about politics"]
N4_REPLY_ID = [OUT_OF_SCOPE_REPLY,
               "Maaf kak, aku cuma bisa bantu seputar Toti Cakery — menu, pemesanan, pembayaran, dan info toko 😊 Ada yang mau ditanyakan soal itu?",
               "Hehe, itu di luar bagianku kak 🙏 Aku asisten toko Toti Cakery, jadi bisanya bantu soal kue, pesanan, dan pembayaran. Mau lihat menu?",
               "Aku nggak bisa jawab yang itu ya kak — tugasku khusus melayani Toti Cakery 😊 Kalau soal menu atau pesanan, gaskeun!",
               "Maaf banget, topik itu bukan wilayahku 🙏 Aku di sini untuk urusan toko Toti Cakery aja. Ada yang bisa kubantu soal kue?",
               "Wah, aku khusus urusan Toti Cakery aja nih kak 😊 Menu, pesan kue, cek pesanan — itu keahlianku. Mau mulai dari mana?"]
N4_REPLY_EN = ["Sorry, I can only help with Toti Cakery things — menu, orders, payment, and delivery 😊 Anything I can help you with there?",
               "That's outside my scope 🙏 I'm Toti Cakery's shop assistant, so I can help with cakes, orders, and payments. Want to see the menu?",
               "I can't answer that one — I only handle Toti Cakery matters 😊 Can I help you with our menu or an order instead?"]

# v4 (§2.4/§3.3): kasus "produk DISEBUT tapi kurang spesifik" (dulu nosize/
# noisi/nocookie/noflav) PINDAH ke T5 add_to_cart dgn argumen verbatim —
# resolver kode yang bertanya. N5 tersisa kasus tanpa produk yang bisa
# diteruskan: noprod (tidak ada kata produk), cmp1 (bandingkan tanpa objek),
# deictic ("yang itu" tanpa referensi), deictic_menu ("yang kedua" setelah
# history menu yang di produksi sudah jadi PENANDA — daftar tidak terlihat
# lagi, model harus tanya nama, bukan menebak).
N5_ID = [("{greet}mau pesan kue{part}", "noprod"), ("{greet}aku mau order{part}", "noprod"),
         ("{greet}mau beli buat ultah besok{part}", "noprod"), ("{greet}pengen kue nih{part}", "noprod"),
         ("{greet}bagusan mana ya kue-kuenya{part}?", "cmp1"), ("{greet}bandingin dong yang enak{part}", "cmp1"),
         ("{greet}yang pertama {qty}{unit} ya{part}", "deictic_menu"),
         ("{greet}aku mau yang kedua{part}", "deictic_menu"),
         ("{greet}ambil yang nomor dua aja{part}", "deictic_menu"),
         ("{greet}yang itu {qty}{unit} ya{part}", "deictic"), ("{greet}yang tadi aja deh{part}", "deictic")]
N5_EN = [("{greet}I want to order a cake{part}", "noprod"), ("{greet}I'd like to place an order", "noprod"),
         ("{greet}which one is better?", "cmp1"),
         ("{greet}the first one please, {qty}", "deictic_menu"),
         ("{greet}I'll go with the second one", "deictic_menu"),
         ("{greet}I'll take that one", "deictic")]
N5_REPLY = {
    ("noprod", "id"): ["Boleh kak! Mau kue yang mana — cupcakes, cake, atau cookies? Ketik *lihat menu* kalau mau lihat pilihannya dulu 😊",
                       "Siap! Kue apa yang mau dipesan kak? Kalau mau lihat pilihan lengkapnya dulu, bilang aja 'menu' ya 😊"],
    ("cmp1", "id"): ["Boleh kubantu bandingkan kak — mau membandingkan kue yang mana saja ya? 😊",
                     "Mau bandingkan yang mana dengan yang mana kak?"],
    ("deictic", "id"): ["Maaf kak, yang dimaksud kue yang mana ya? 😊",
                        "Hehe, aku belum nangkep yang mana kak — boleh sebutkan nama kuenya?"],
    ("deictic_menu", "id"): ["Biar nggak salah pilih, boleh sebutkan nama kuenya kak? 😊",
                             "Siap! Yang mana ya kak — boleh tulis nama kuenya? 😊"],
    ("noprod", "en"): ["Sure! Which one would you like — cupcakes, cake, or cookies? 😊"],
    ("cmp1", "en"): ["Happy to compare — which two items should I compare? 😊"],
    ("deictic", "en"): ["Sorry, which item do you mean? 😊"],
    ("deictic_menu", "en"): ["To make sure I get it right — which cake is that? Please type its name 😊"],
}

N6_ID = [("{greet}kemarin aku beli {prod} {qty}{unit}, enak banget{part}!", "past"),
         ("{greet}dulu pernah nyobain {prod} di sini, masih sama enaknya ga ya{part}?", "past"),
         ("{greet}kalau misal aku pesan 100 {cat} kalian sanggup ga{part}?", "capacity"),
         ("{greet}seandainya order buat 500 orang, bisa ga{part}?", "capacity"),
         ("{greet}cara batalin pesanan gimana{part}?", "howcancel"),
         ("{greet}gimana sih cara pesannya{part}?", "howorder"),
         ("{greet}kalau mau ubah pesanan caranya gimana{part}?", "howcancel"),
         ("{greet}ga jadi mau {prod} deh, cuma tanya-tanya aja{part}", "negated"),
         ("{greet}batal nanya deh hehe{part}", "negated"),
         ("{greet}resep {cat} yang enak dong{part}", "recipe")]
N6_EN = [("{greet}I bought {qty} {prod} last week, they were amazing!", "past"),
         ("{greet}hypothetically, could you handle an order of 100 {cat}?", "capacity"),
         ("{greet}how do I cancel an order?", "howcancel"),
         ("{greet}how does ordering work here?", "howorder"),
         ("{greet}never mind the {prod}, I was just asking", "negated")]
N6_REPLY = {
    ("past", "id"): ["Wah, makasih banyak kak! Senang banget kalau suka 😊 Mau pesan lagi atau lihat menu terbaru?",
                     "Terima kasih kak, jadi semangat! Kalau mau nostalgia rasanya lagi, aku siap bantu pesankan 😄"],
    ("capacity", "id"): ["Untuk jumlah sebesar itu perlu dicek dulu kapasitas produksinya kak 🙏 Mau aku teruskan ke admin biar dihitungkan?",
                         "Wah, seru! Tapi order sebesar itu perlu koordinasi dengan tim dulu ya kak. Mau kusambungkan ke admin?"],
    ("howcancel", "id"): ["Gampang kak — selama pesanan belum dibayar, cukup bilang 'batalkan pesananku' di chat ini. Mau kubatalkan sekarang? 😊",
                          "Untuk ubah/batal pesanan yang belum dibayar, tinggal bilang di chat ini aja kak. Perlu kubantu sekarang?"],
    ("howorder", "id"): ["Gampang banget kak: sebutkan kue dan jumlahnya di chat ini (misal 'mau cupcakes isi 6 cokelat 2'), nanti kubantu sampai pembayaran 😊",
                         "Cukup chat aku aja kak — sebut kue + jumlah, nanti aku rangkum pesanannya dan kirimkan cara bayarnya 😊"],
    ("negated", "id"): ["Oke kak, santai aja 😊 Kalau nanti jadi pengen, tinggal chat lagi ya!",
                        "Siap, nggak apa-apa kak! Aku di sini kalau butuh info atau mau pesan 😊"],
    ("recipe", "id"): ["Hehe, resepnya rahasia dapur kami kak 🙏 Tapi kalau mau yang jadi tinggal santap, aku bisa bantu pesankan 😄",
                       "Wah, kalau resep aku nggak bisa bagikan ya kak 😊 Yang bisa kubantu: pesan kuenya langsung dari Toti Cakery!"],
    ("past", "en"): ["Thank you so much! Glad you loved them 😊 Want to order again or see the latest menu?"],
    ("capacity", "en"): ["An order that big needs a production check first 🙏 Want me to forward it to our admin?"],
    ("howcancel", "en"): ["Easy — as long as the order is unpaid, just tell me 'cancel my order' here. Want me to cancel one now? 😊"],
    ("howorder", "en"): ["Just chat with me: mention the cake and quantity (e.g. '2 chocolate cupcakes isi 6') and I'll guide you to payment 😊"],
    ("negated", "en"): ["No worries! I'm here whenever you feel like ordering 😊"],
}

# ── Composition tables ───────────────────────────────────────────────────────
# v2 (2026-07-05): N5/N6/T10 diperbanyak — verdict harness v1 (toti-qwen-1.7b
# 4/5 target) menunjukkan regresi terlokalisir di N5 ambigu (2/7), N6 jebakan
# (3/5), dan T10 escalate (2/4). Split TEST DIBEKUKAN dari v1 (lihat main()).
# v3 (2026-07-06): T5 60->90 (single-item add_to_cart when flavour IS given ->
# add directly, don't over-clarify), T11 12->30 & T12 13->30 (owner reports were
# 0/3 in the v2 wired eval — thin slice under-taught). Test split stays frozen.
# v4 (2026-07-16, PROMPT_FINETUNE_V4 §3): T3 70->90 (insiden #2, frasa detail),
# T5 90->140 (termasuk order generik verbatim — insiden #4; menyerap eks-N5
# nosize/noisi/nocookie/noflav), N5 110->70 (tersisa noprod/cmp1/deictic),
# T7 35->25 (varian "menu" dihapus: daftar menu di history kini penanda, pilihan
# "yang kedua" tak bisa di-resolve -> jadi N5 deictic_menu). MT naik di T1/T3/T5
# utk melatih history tercemar penanda (insiden #3). Test split TETAP dibekukan.
TRAIN_COUNTS = {"T1": 70, "T2": 30, "T3": 90, "T4": 40, "T5": 140, "T6": 35, "T7": 25,
                "T8": 50, "T9": 30, "T10": 60, "T11": 30, "T12": 30,
                "N1": 90, "N2": 25, "N3": 50, "N4": 60, "N5": 70, "N6": 80}
VAL_COUNTS = {"T1": 7, "T2": 3, "T3": 9, "T4": 4, "T5": 14, "T6": 4, "T7": 3,
              "T8": 5, "T9": 3, "T10": 6, "T11": 3, "T12": 3,
              "N1": 9, "N2": 3, "N3": 5, "N4": 6, "N5": 7, "N6": 8}
TEST_COUNTS = {"T1": 9, "T2": 4, "T3": 9, "T4": 5, "T5": 7, "T6": 4, "T7": 4,
               "T8": 6, "T9": 4, "T10": 4, "T11": 2, "T12": 2,
               "N1": 11, "N2": 3, "N3": 6, "N4": 8, "N5": 7, "N6": 5}
EN_SHARE = {"T1": .2, "T2": .2, "T3": .2, "T4": .2, "T5": .2, "T6": .2, "T7": .2,
            "T8": .2, "T9": .2, "T10": .2, "T11": .25, "T12": .25,
            "N1": .25, "N2": .2, "N3": .25, "N4": .25, "N5": .2, "N6": .2}
MT_SHARE = {"T1": .35, "T2": .2, "T3": .45, "T4": .25, "T5": .3, "T6": .2, "T7": 1.0,
            "T8": .3, "T9": .4, "T10": .3, "T11": .1, "T12": .1,
            "N1": .3, "N2": .2, "N3": .3, "N4": .25, "N5": .3, "N6": .4}

assert sum(TRAIN_COUNTS.values()) == 1005 and sum(VAL_COUNTS.values()) == 102
assert sum(TEST_COUNTS.values()) == 100


# ── Engine ────────────────────────────────────────────────────────────────────
class Gen:
    def __init__(self):
        self.rng = random.Random(42)
        self.used_user_texts: dict[str, str] = {}   # text -> split
        self.tpl_usage: Counter = Counter()
        self.product_usage: Counter = Counter()
        self.rows = {"train": [], "validation": [], "test": []}

    # template pools are split once: last max(2, 15%) entries -> test-only.
    # Tiny pools (<6) hold out 1; single-template pools are shared (no holdout).
    @staticmethod
    def pool_split(pool):
        if len(pool) == 1:
            return pool, pool
        n_test = max(2, round(len(pool) * 0.15)) if len(pool) >= 6 else 1
        n_test = min(n_test, len(pool) - 1)
        return pool[:-n_test], pool[-n_test:]

    def pick_tpl(self, pool_name, pool, split, total_count):
        cap = math.ceil(total_count / max(1, len(pool))) + 2
        candidates = [i for i in range(len(pool)) if self.tpl_usage[(pool_name, split, i)] < cap]
        if not candidates:
            candidates = list(range(len(pool)))
        i = self.rng.choice(candidates)
        self.tpl_usage[(pool_name, split, i)] += 1
        return pool[i]

    def pick_product(self, split, category=None, exclude=()):
        opts = [p for p, (c, _) in MENU.items()
                if (category is None or c == category) and p not in exclude]
        if split != "test":
            opts = [p for p in opts if p not in HOLDOUT_PRODUCTS]
        # usage-balanced pick
        pick_from = self.rng.sample(opts, k=min(3, len(opts)))
        p = min(pick_from, key=lambda x: self.product_usage[x])
        self.product_usage[p] += 1
        return p

    def pick_flavour(self, product, split):
        flavs = MENU[product][1]
        if split != "test":
            flavs = [f for f in flavs if f != HOLDOUT_FLAVOUR]
        else:
            # bias toward holdout flavour on cookies rows in test
            if HOLDOUT_FLAVOUR in flavs and self.rng.random() < 0.5:
                return HOLDOUT_FLAVOUR
        return self.rng.choice(flavs)

    def surface(self, product, flavour, lang):
        base = self.rng.choice(BASE_SURFACES[product])
        if flavour is None:
            return base
        if lang == "en":
            f = self.rng.choice(FLAV_SURFACE_EN[flavour])
            return f"{f} {base}"
        f = self.rng.choice(FLAV_SURFACE_ID[flavour])
        return f"{base} {f}" if self.rng.random() < 0.7 else f"{base} {f}"

    def canonical(self, product, flavour):
        return f"{product} {flavour}" if flavour else product

    def slots(self, lang):
        if lang == "en":
            return {"greet": self.rng.choice(GREET_EN), "part": self.rng.choice(PART_EN)}
        return {"greet": self.rng.choice(GREET_ID), "part": self.rng.choice(PART_ID)}

    def qty(self, lang):
        return self.rng.choice(QTY_EN if lang == "en" else QTY_ID)

    # Fictional but stable placeholder prices (real prices come from tools at
    # runtime; these only appear inside simulated history turns).
    PRICE_TABLE = {"Cupcakes isi 4": 60_000, "Cupcakes isi 6": 85_000,
                   "Cupcakes isi 9": 120_000, "Cupcakes Tart isi 7": 110_000,
                   "Cake 10cm": 100_000, "Cake 15cm": 150_000, "Cake 18cm": 195_000,
                   "Cake 20cm": 235_000, "Cake 22cm": 275_000,
                   "Mini Cookies 7cm": 35_000, "Bento Cookies 10cm": 55_000,
                   "Giant Cookies 15cm": 95_000}

    def fic_price(self, product_full=None):
        if product_full:
            for base, price in self.PRICE_TABLE.items():
                if product_full.startswith(base):
                    return price
        return self.rng.randrange(15_000, 250_001, 5_000)

    # ── history pair builders (assistant text mimics runtime outputs) ─────────
    def h_menu(self, products_full):
        lines = ["Berikut menu Toti Cakery:"]
        for p in products_full:
            lines.append(f"• {p} — {rupiah(self.fic_price(p))}")
        lines.append("\nMau lihat detail salah satu kue? Sebutkan namanya ya 😊")
        user = self.rng.choice(["menu dong", "liat menu ya kak", "ada kue apa aja?", "menu please"])
        return [user, "\n".join(lines)]

    def h_detail(self, product_full):
        desc = self.rng.choice(["Lembut, manis pas, dan selalu fresh dari oven kami.",
                                "Favorit pelanggan — teksturnya lembut dengan rasa yang kaya.",
                                "Dibuat fresh setiap hari dengan bahan pilihan."])
        text = (f"*{product_full}*\n{desc}\nHarga: {rupiah(self.fic_price(product_full))}\n\n"
                "Mau pesan ini? Bilang aja jumlahnya ya 😊")
        user = self.rng.choice([f"detail {product_full.lower()} dong", f"{product_full.lower()} kayak gimana?",
                                f"tell me about the {product_full.lower()}"])
        return [user, text]

    def h_cart(self, items):
        cart = [{"nama": n, "qty": q, "harga": float(self.fic_price(n))} for n, q in items]
        text = (cart_summary(cart)
                + "\n\nSudah sesuai semua, atau mau nambah lagi? Ketik *sudah sesuai* untuk lanjut ya 😊")
        user = self.rng.choice([f"mau pesan {items[0][0].lower()} {items[0][1]}",
                                f"order {items[0][0].lower()} {items[0][1]} ya"])
        return [user, text]

    def h_status(self):
        text = (f"Status pesanan *INV-2026{self.rng.randrange(1000, 9999)}*: Sedang diproses "
                "(pembayaran: lunas)\nJumlah item: 1\nTotal: " + rupiah(self.fic_price()))
        user = self.rng.choice(["status pesananku dong", "orderku gimana?"])
        return [user, text]

    def h_chat(self):
        pairs = [("halo kak", "Halo! Selamat datang di Toti Cakery 😊 Mau lihat menu atau pesan sesuatu?"),
                 ("makasih infonya", "Sama-sama kak! Kalau butuh apa-apa lagi, chat aja ya 😊"),
                 ("kalian buka sampai jam berapa?", "Toti Cakery buka Senin-Sabtu pukul 09.00 - 19.00 WIB ya kak 😊")]
        return list(self.rng.choice(pairs))

    def history(self, kind_pool, n_pairs):
        pairs, seen_replies = [], set()
        attempts = 0
        while len(pairs) < n_pairs and attempts < n_pairs * 6:
            attempts += 1
            kind = self.rng.choice(kind_pool)
            if kind == "menu":
                ps = [self.canonical(self.pick_product("train"), self.rng.choice(CUP_FLAV))
                      for _ in range(2)]
                pair = self.h_menu(ps)
            elif kind == "detail":
                p = self.pick_product("train")
                pair = self.h_detail(self.canonical(p, self.rng.choice(MENU[p][1][:2])))
            elif kind == "chat":
                pair = self.h_chat()
            else:
                pair = self.h_status()
            if pair[1] in seen_replies:  # never repeat an assistant line in one history
                continue
            seen_replies.add(pair[1])
            pairs.append(pair)
        msgs = []
        for u, a in pairs:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        return msgs

    # ── row assembly ──────────────────────────────────────────────────────────
    @staticmethod
    def _norm(text):
        return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()

    def _register_text(self, text, split):
        key = self._norm(text)
        if key in self.used_user_texts:
            return False
        self.used_user_texts[key] = split
        return True

    def make_row(self, split, rtype, lang, history, user_text, final_turn, system=SYSTEM_PROMPT,
                 noised=False):
        # v4 §3.4 — history meniru produksi: balasan bot lama masuk konteks
        # lewat _history_view() runtime (menu/detail -> penanda ringkas, teks
        # panjang dipotong). Satu titik jepit utk semua tipe baris.
        history = [m if m["role"] == "user"
                   else {"role": "assistant", "content": _history_view(m["content"])}
                   for m in history]
        # v4 §3.5 — reminder menempel di ujung system content, PERSIS seperti
        # yang dilihat model di serving (Ollama meng-collate semua system
        # message ke blok teratas, digabung "\n\n").
        messages = [{"role": "system", "content": system + "\n\n" + TOOL_REMINDER}] + history
        messages.append({"role": "user", "content": user_text})
        messages.append(final_turn)
        meta = {"type": rtype, "lang": lang, "multi_turn": bool(history), "noised": noised}
        return {"messages": messages, "tools_json": json.dumps(TOOLS, ensure_ascii=False), "meta": meta}

    def tool_turn(self, name, args_obj):
        assert name in TOOL_NAMES
        return {"role": "assistant", "content": "",
                "tool_calls": [{"type": "function", "function": {
                    "name": name,
                    "arguments": json.dumps(args_obj, ensure_ascii=False, separators=(",", ":"))}}]}

    def text_turn(self, text):
        assert text and not PRICE_RE.search(text), f"price/stock leak in reply: {text!r}"
        return {"role": "assistant", "content": text}

    def plan(self, count, en_share, mt_share):
        n_en = round(count * en_share)
        n_mt = round(count * mt_share)
        langs = ["en"] * n_en + ["id"] * (count - n_en)
        mts = [True] * n_mt + [False] * (count - n_mt)
        self.rng.shuffle(langs)
        self.rng.shuffle(mts)
        return list(zip(langs, mts))

    def unique_user(self, build_fn, split, max_tries=120):
        suffixes = [" kak", " ya", " yaa", " hehe", " deh", " kk", " 🙂", " dong", " nih", " pls", " ^^"]
        for attempt in range(max_tries):
            out = build_fn()
            if out is None:
                continue
            text = out[0]
            if attempt >= 30:  # stuck: add a natural filler suffix for variety
                text = text.rstrip() + self.rng.choice(suffixes)
            if self._register_text(text, split):
                return (text,) + tuple(out[1:])
        raise RuntimeError("could not build unique user text")

    def maybe_noise(self, text, lang, flag):
        return _typo_ops(self.rng, text) if flag else text

    # ── per-type generators ──────────────────────────────────────────────────
    def gen_type(self, rtype, split, count):
        pools = self._pools(rtype, split)
        plan = self.plan(count, EN_SHARE[rtype], MT_SHARE[rtype])
        # exact noise plan: 25% of ID rows / 15% of EN rows
        id_idx = [i for i, (l, _) in enumerate(plan) if l == "id"]
        en_idx = [i for i, (l, _) in enumerate(plan) if l == "en"]
        noise_set = set(self.rng.sample(id_idx, round(len(id_idx) * 0.25)) if id_idx else [])
        noise_set |= set(self.rng.sample(en_idx, round(len(en_idx) * 0.15)) if en_idx else [])
        for i, (lang, mt) in enumerate(plan):
            row = self._build(rtype, split, lang, mt, i in noise_set, pools, count)
            self.rows[split].append(row)

    def _pools(self, rtype, split):
        """Return the (user pools etc.) for main (train+val) vs test regimes."""
        regime = "test" if split == "test" else "main"

        def sel(pool):
            main, test = self.pool_split(pool)
            return test if regime == "test" else main

        P = {"regime": regime, "sel": sel}
        return P

    def _hist_for(self, rtype, lang):
        n = self.rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]
        # v4: "menu"/"detail" = history tercemar penanda (insiden #3) — dibiaskan
        # kuat di T1/T3/T5: model harus TETAP memanggil tool walau penanda bilang
        # data pernah ditampilkan.
        kind_pool = {"T9": ["status", "chat"], "N6": ["menu", "chat"],
                     "N1": ["chat"], "N3": ["chat"], "N4": ["chat"], "N2": ["chat"],
                     "T1": ["menu", "menu", "chat", "status"],
                     "T3": ["menu", "detail", "detail", "chat"],
                     "T5": ["menu", "detail", "chat"],
                     }.get(rtype, ["chat", "menu", "status"])
        return self.history(kind_pool, n)

    def _build(self, rtype, split, lang, mt, noise, P, total_count):
        sel = P["sel"]
        rng = self.rng
        history = self._hist_for(rtype, lang) if (mt and rtype != "T7") else []
        s = self.slots(lang)

        def uniq(fn):
            return self.unique_user(fn, split)

        if rtype == "T1":
            pool = sel(T1_EN if lang == "en" else T1_ID)

            def build():
                tpl = self.pick_tpl(f"T1u{lang}", pool, P["regime"], total_count)
                sl = dict(self.slots(lang))
                if "{prod}" in tpl:
                    p = self.pick_product(split)
                    fl = self.pick_flavour(p, split) if rng.random() < 0.4 else None
                    sl["prod"] = self.surface(p, fl, lang)
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return (text,)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("get_menu", {}), noised=noise)

        if rtype == "T2":
            pool = sel(T2_EN if lang == "en" else T2_ID)
            cats = CATEGORY_SURFACES_EN if lang == "en" else CATEGORY_SURFACES_ID

            def build():
                tpl = self.pick_tpl(f"T2u{lang}", pool, P["regime"], total_count)
                cat_s, cat_c = rng.choice(cats)
                text = self.maybe_noise(render(tpl, cat=cat_s, **self.slots(lang)), lang, noise)
                return text, cat_c
            text, cat_c = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("get_menu", {"kategori": cat_c}), noised=noise)

        if rtype == "T3":
            pool = sel(T3_EN if lang == "en" else T3_ID)

            def build():
                tpl = self.pick_tpl(f"T3u{lang}", pool, P["regime"], total_count)
                p = self.pick_product(split)
                fl = self.pick_flavour(p, split) if rng.random() < 0.5 else None
                surf = self.surface(p, fl, lang)  # v4 §3.3: argumen = kata pelanggan
                text = self.maybe_noise(
                    render(tpl, prod=surf, **self.slots(lang)), lang, noise)
                return text, surf
            text, surf = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("get_product_detail", {"product": surf}), noised=noise)

        if rtype == "T4":
            pool = sel(T4_EN if lang == "en" else T4_ID)

            def build():
                tpl = self.pick_tpl(f"T4u{lang}", pool, P["regime"], total_count)
                n = 3 if "{prodC}" in tpl else 2
                prods, picked = [], []
                for _ in range(n):
                    p = self.pick_product(split, exclude=tuple(picked))
                    picked.append(p)
                    fl = self.pick_flavour(p, split) if rng.random() < 0.3 else None
                    prods.append(self.surface(p, fl, lang))  # v4 §3.3: verbatim
                sl = dict(self.slots(lang))
                sl.update({"prodA": prods[0], "prodB": prods[1]})
                if n == 3:
                    sl["prodC"] = prods[2]
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return text, prods
            text, prods = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("compare_products", {"products": prods}), noised=noise)

        if rtype == "T5":
            pool_full = T5_EN if lang == "en" else T5_ID
            noqty = T5_EN_NOQTY if lang == "en" else T5_ID_NOQTY
            pool = sel(pool_full)

            def build():
                tpl = self.pick_tpl(f"T5u{lang}", pool, P["regime"], total_count)
                idx = pool_full.index(tpl)
                # v4 §3.3 (insiden #4): argumen = kata pelanggan APA ADANYA.
                # ~25% order generik ("beli 4 cupcake" -> product:"cupcake"),
                # ~15% produk tanpa flavour, sisanya lengkap dgn flavour —
                # semuanya verbatim; resolver kode yang memutuskan/bertanya.
                r = rng.random()
                if r < 0.25:
                    surf = rng.choice(GENERIC_SURFACES)
                elif r < 0.40:
                    surf = self.surface(self.pick_product(split), None, lang)
                else:
                    p = self.pick_product(split)
                    surf = self.surface(p, self.pick_flavour(p, split), lang)
                sl = dict(self.slots(lang))
                sl["prod"] = surf
                if idx in noqty:
                    q = 1
                else:
                    qs, q = self.qty(lang)
                    sl["qty"] = qs
                    sl["unit"] = rng.choice(UNITS_ID) if lang == "id" else ""
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return text, surf, q
            text, surf, q = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("add_to_cart", {"items": [{"product": surf, "qty": q}]}),
                                 noised=noise)

        if rtype == "T6":
            pool = sel(T6_EN if lang == "en" else T6_ID)
            conns = [" sama ", " dan ", " plus ", ", terus "] if lang == "id" else [" and ", " plus ", ", and "]

            def build():
                tpl = self.pick_tpl(f"T6u{lang}", pool, P["regime"], total_count)
                n = rng.choice([2, 2, 3])
                items, parts, seen = [], [], set()
                for _ in range(n):
                    p = self.pick_product(split, exclude=tuple(seen))
                    seen.add(p)
                    fl = self.pick_flavour(p, split)
                    qs, q = self.qty(lang)
                    surf = self.surface(p, fl, lang)  # v4 §3.3: verbatim
                    if lang == "id":
                        parts.append(f"{surf} {qs}")
                    else:
                        parts.append(f"{qs} {surf}")
                    items.append({"product": surf, "qty": q})
                items_str = ""
                for j, part in enumerate(parts):
                    items_str += part if j == 0 else rng.choice(conns) + part
                text = self.maybe_noise(render(tpl, items=items_str, **self.slots(lang)), lang, noise)
                return text, items
            text, items = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("add_to_cart", {"items": items}), noised=noise)

        if rtype == "T7":
            variant = rng.choice(["detail", "add"])
            p = self.pick_product(split)
            fl = self.pick_flavour(p, split)
            canon = self.canonical(p, fl)
            qs, q = self.qty(lang)
            sl = dict(self.slots(lang))
            sl.update({"qty": qs, "unit": rng.choice(UNITS_ID) if lang == "id" else ""})
            lead = self.history(["chat"], rng.choice([0, 0, 1]))
            if variant == "detail":
                pool = sel(T7_DETAIL_EN if lang == "en" else T7_DETAIL_ID)
                hist = lead + [{"role": "user", "content": self.h_detail(canon)[0]},
                               {"role": "assistant", "content": self.h_detail(canon)[1]}]
                tpl = self.pick_tpl(f"T7d{lang}", pool, P["regime"], total_count)
                # nama produk tersedia di penanda detail history — model
                # meneruskan nama itu, bukan mengarang varian lain (v4 §3.3)
                items = [{"product": canon, "qty": q}]
            else:
                pool = sel(T7_ADD_EN if lang == "en" else T7_ADD_ID)
                pnew = self.pick_product(split, exclude=(p,))
                flnew = self.pick_flavour(pnew, split)
                u, a = self.h_cart([(canon, rng.choice([1, 2]))])
                hist = lead + [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
                tpl = self.pick_tpl(f"T7a{lang}", pool, P["regime"], total_count)
                surf_new = self.surface(pnew, flnew, lang)  # v4 §3.3: verbatim
                sl["prod"] = surf_new
                items = [{"product": surf_new, "qty": q}]

            def build():
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return (text,)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, hist, text,
                                 self.tool_turn("add_to_cart", {"items": items}), noised=noise)

        if rtype == "T8":
            pool = sel(T8_EN if lang == "en" else T8_ID)

            def build():
                tpl = self.pick_tpl(f"T8u{lang}", pool, P["regime"], total_count)
                return (self.maybe_noise(render(tpl, **self.slots(lang)), lang, noise),)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("get_order_status", {}), noised=noise)

        if rtype == "T9":
            pool = sel(T9_EN if lang == "en" else T9_ID)

            def build():
                tpl = self.pick_tpl(f"T9u{lang}", pool, P["regime"], total_count)
                return (self.maybe_noise(render(tpl, **self.slots(lang)), lang, noise),)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("cancel_order", {}), noised=noise)

        if rtype == "T10":
            pool = sel(T10_EN if lang == "en" else T10_ID)

            def build():
                tpl, reason_tpl = self.pick_tpl(f"T10u{lang}", pool, P["regime"], total_count)
                theme = rng.choice(CUSTOM_THEMES)
                complaint = rng.choice(COMPLAINTS)
                tiers, tiers_en = rng.choice([("dua", "two"), ("tiga", "three"), ("empat", "four")])
                text = self.maybe_noise(
                    render(tpl, theme=theme, complaint=complaint, tiers=tiers, tiers_en=tiers_en,
                           **self.slots(lang)), lang, noise)
                reason = reason_tpl.format(theme=theme, complaint=complaint, tiers=tiers)
                assert 5 <= len(reason.split()) <= 15
                return text, reason
            text, reason = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn("escalate_to_admin", {"reason": reason}), noised=noise)

        if rtype in ("T11", "T12"):
            pool_map = {"T11": (T11_ID, T11_EN), "T12": (T12_ID, T12_EN)}
            pid, pen = pool_map[rtype]
            pool = sel(pen if lang == "en" else pid)
            tool = "financial_report" if rtype == "T11" else "business_analytics"

            def build():
                tpl = self.pick_tpl(f"{rtype}u{lang}", pool, P["regime"], total_count)
                return (self.maybe_noise(render(tpl, **self.slots(lang)), lang, noise),)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.tool_turn(tool, {}), noised=noise)

        if rtype == "N1":
            def build():
                if split == "test" and rng.random() < 0.4:
                    docs_ok = [d for d in FAQ_DOCS if d["id"] in TEST_ONLY_DOCS]
                else:
                    docs_ok = [d for d in FAQ_DOCS
                               if split == "test" or d["id"] not in TEST_ONLY_DOCS]
                doc = rng.choice(docs_ok)
                qpool = doc["q_en"] if lang == "en" else doc["q_id"]
                qt = rng.choice(qpool)
                text = self.maybe_noise(render(qt, **self.slots(lang)), lang, noise)
                return text, doc
            text, doc = uniq(build)
            distract = rng.sample([d for d in FAQ_DOCS if d is not doc
                                   and (split == "test" or d["id"] not in TEST_ONLY_DOCS)],
                                  k=rng.choice([0, 1, 2]))
            block = [doc["doc"]] + [d["doc"] for d in distract]
            rng.shuffle(block)
            system = SYSTEM_PROMPT + FAQ_HEADER + DOC_SEP.join(block)
            apool = doc["a_en"] if lang == "en" else doc["a_id"]
            answer = rng.choice(apool)
            if lang == "id":
                assert _grounded(answer, doc["doc"]), f"ungrounded N1 answer for {doc['id']}"
            return self.make_row(split, rtype, lang, history, text,
                                 self.text_turn(answer), system=system, noised=noise)

        if rtype == "N2":
            pool = sel(N2_EN if lang == "en" else N2_ID)
            rpool = sel(N2_REPLY_EN if lang == "en" else N2_REPLY_ID)

            def build():
                tpl, _kind = self.pick_tpl(f"N2u{lang}", pool, P["regime"], total_count)
                text = self.maybe_noise(
                    render(tpl, city=rng.choice(CITIES), **self.slots(lang)), lang, noise)
                return (text,)
            (text,) = uniq(build)
            return self.make_row(split, rtype, lang, history, text,
                                 self.text_turn(rng.choice(rpool)), noised=noise)

        if rtype == "N3":
            pool = sel(N3_EN if lang == "en" else N3_ID)

            def build():
                tpl, kind = self.pick_tpl(f"N3u{lang}", pool, P["regime"], total_count)
                text = self.maybe_noise(render(tpl, **self.slots(lang)), lang, noise)
                return text, kind
            text, kind = uniq(build)
            hist_texts = {m["content"] for m in history if m["role"] == "assistant"}
            options = [x for x in N3_REPLY[(kind, lang)] if x not in hist_texts] or N3_REPLY[(kind, lang)]
            reply = rng.choice(options)
            return self.make_row(split, rtype, lang, history, text,
                                 self.text_turn(reply), noised=noise)

        if rtype == "N4":
            pool = sel(N4_EN if lang == "en" else N4_ID)
            rpool = sel(N4_REPLY_EN if lang == "en" else N4_REPLY_ID)

            def build():
                tpl = self.pick_tpl(f"N4u{lang}", pool, P["regime"], total_count)
                return (self.maybe_noise(render(tpl, **self.slots(lang)), lang, noise),)
            (text,) = uniq(build)
            reply = rng.choice(rpool)
            assert ("Toti Cakery" in reply) or ("toko" in reply.lower()) or ("shop" in reply.lower())
            return self.make_row(split, rtype, lang, history, text,
                                 self.text_turn(reply), noised=noise)

        if rtype == "N5":
            pool = sel(N5_EN if lang == "en" else N5_ID)

            def build():
                tpl, kind = self.pick_tpl(f"N5u{lang}", pool, P["regime"], total_count)
                sl = dict(self.slots(lang))
                if "{qty}" in tpl:
                    qs, _ = self.qty(lang)
                    sl["qty"] = qs
                    sl["unit"] = rng.choice(UNITS_ID) if lang == "id" else ""
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return text, kind
            text, kind = uniq(build)
            if kind == "deictic_menu":
                # WAJIB ber-history penanda menu: item tak terlihat lagi, "yang
                # kedua" tak bisa di-resolve siapa pun -> tanya nama (insiden #3/#4)
                hist = self.history(["menu"], 1)
            elif kind == "deictic":
                # deictic polos: history apa pun TIDAK boleh me-resolve rujukan
                hist = self.history(["chat"], 1) if mt else []
            else:
                hist = self.history(["chat"], 1) if mt else []
            reply = rng.choice(N5_REPLY[(kind, lang)])
            assert reply.count("?") == 1, f"N5 reply must have exactly one '?': {reply!r}"
            return self.make_row(split, rtype, lang, hist, text,
                                 self.text_turn(reply), noised=noise)

        if rtype == "N6":
            pool = sel(N6_EN if lang == "en" else N6_ID)

            def build():
                tpl, kind = self.pick_tpl(f"N6u{lang}", pool, P["regime"], total_count)
                sl = dict(self.slots(lang))
                if "{prod}" in tpl:
                    p = self.pick_product(split)
                    fl = self.pick_flavour(p, split) if rng.random() < 0.5 else None
                    sl["prod"] = self.surface(p, fl, lang)
                if "{qty}" in tpl:
                    qs, _ = self.qty(lang)
                    sl["qty"] = qs
                    sl["unit"] = rng.choice(UNITS_ID) if lang == "id" else ""
                if "{cat}" in tpl:
                    sl["cat"] = rng.choice(["cupcakes", "cookies", "cake"])
                text = self.maybe_noise(render(tpl, **sl), lang, noise)
                return text, kind
            text, kind = uniq(build)
            reply = rng.choice(N6_REPLY[(kind, lang)])
            return self.make_row(split, rtype, lang, history, text,
                                 self.text_turn(reply), noised=noise)

        raise ValueError(rtype)


# ── Validation of generated rows (self-check) ────────────────────────────────
def _validate_args(name: str, obj: dict) -> None:
    if name in ("get_order_status", "cancel_order", "financial_report", "business_analytics"):
        assert obj == {}, (name, obj)
    elif name == "get_menu":
        assert set(obj) <= {"kategori"}, obj
        if "kategori" in obj:
            assert obj["kategori"] in ("cupcake", "cake", "cookies"), obj
    elif name == "get_product_detail":
        assert set(obj) == {"product"} and isinstance(obj["product"], str) and obj["product"], obj
    elif name == "compare_products":
        assert set(obj) == {"products"} and len(obj["products"]) >= 2, obj
        assert all(isinstance(p, str) for p in obj["products"]), obj
    elif name == "add_to_cart":
        assert set(obj) == {"items"} and obj["items"], obj
        for it in obj["items"]:
            assert set(it) == {"product", "qty"} and isinstance(it["qty"], int) and it["qty"] >= 1, it
    elif name == "escalate_to_admin":
        assert set(obj) == {"reason"} and 5 <= len(obj["reason"].split()) <= 15, obj
    else:
        raise AssertionError(name)


def self_check(rows_by_split):
    for split, rows in rows_by_split.items():
        # test dibekukan dari v1 (format lama: tanpa reminder, history literal,
        # argumen kanonik) — aturan v4 hanya berlaku utk train/validation.
        v4 = split != "test"
        for row in rows:
            msgs = row["messages"]
            sysc = msgs[0]["content"]
            assert msgs[0]["role"] == "system" and sysc
            if v4:
                assert sysc.startswith(SYSTEM_PROMPT)
                suffix = "\n\n" + TOOL_REMINDER
                assert sysc.endswith(suffix), (split, sysc[-80:])
                mid = sysc[len(SYSTEM_PROMPT):-len(suffix)]
                assert mid == "" or mid.startswith(FAQ_HEADER)
            # test beku menyimpan system prompt era v1 (prompt produksi sudah
            # dipatch sejak itu) — harness merakit ulang dari SYSTEM_PROMPT
            # terkini saat replay, jadi di sini cukup cek struktur.
            body = msgs[1:]
            assert len(body) % 2 == 0 and len(body) <= 8
            for j, m in enumerate(body):
                assert m["role"] == ("user" if j % 2 == 0 else "assistant"), (split, j)
            if v4:
                # §3.1/§3.4: balasan bot lama di history harus sudah jadi penanda/
                # potongan _history_view — menu literal & blok harga DILARANG.
                for m in body[:-1]:
                    if m["role"] == "assistant":
                        c = m["content"]
                        assert "Berikut menu" not in c and "Harga:" not in c, (split, c[:60])
            final = body[-1]
            if "tool_calls" in final:
                assert final["content"] == "" and len(final["tool_calls"]) == 1
                fn = final["tool_calls"][0]["function"]
                obj = json.loads(fn["arguments"])
                _validate_args(fn["name"], obj)
                # §3.3: argumen produk = kata-kata yang benar-benar ada di
                # percakapan (teks user atau penanda history) — bukan karangan.
                # Baris noised dikecualikan (typo mengubah substring).
                if v4 and not row["meta"]["noised"]:
                    hay = " ".join(m["content"] for m in body
                                   if isinstance(m.get("content"), str)).lower()
                    prods = ([it["product"] for it in obj.get("items", [])]
                             + ([obj["product"]] if "product" in obj else [])
                             + list(obj.get("products", [])))
                    for s in prods:
                        assert s.lower() in hay, (split, s, hay[:120])
            else:
                assert final["content"] and not PRICE_RE.search(final["content"]), final
            # holdout products/flavour never outside test (args + meta scope)
            if split != "test" and "tool_calls" in final:
                args_l = final["tool_calls"][0]["function"]["arguments"].lower()
                for hp in HOLDOUT_PRODUCTS | {HOLDOUT_FLAVOUR}:
                    assert hp.lower() not in args_l, (split, args_l)


def stats(rows_by_split):
    out = {}
    for split, rows in rows_by_split.items():
        c = Counter(r["meta"]["type"] for r in rows)
        langs = Counter(r["meta"]["lang"] for r in rows)
        mt = sum(1 for r in rows if r["meta"]["multi_turn"])
        noised = sum(1 for r in rows if r["meta"]["noised"])
        tool_rows = sum(v for k, v in c.items() if k.startswith("T"))
        out[split] = {"total": len(rows), "per_type": dict(sorted(c.items())),
                      "tool_rows": tool_rows, "non_tool_rows": len(rows) - tool_rows,
                      "en_share": round(langs["en"] / len(rows), 3),
                      "multi_turn_share": round(mt / len(rows), 3),
                      "noised_share": round(noised / len(rows), 3)}
    return out


def main():
    g = Gen()

    # Split test DIBEKUKAN dari v1: hasil eval (baseline & v1) hanya sebanding
    # kalau test byte-identik. RNG generator tunggal, jadi test TIDAK digenerate
    # ulang; teks user-nya diregistrasikan agar train/val v2 tetap bebas overlap.
    frozen_path = OUT_DIR / "test.jsonl"
    frozen_test = None
    if frozen_path.exists():
        frozen_test = [json.loads(l) for l in frozen_path.open(encoding="utf-8")]
        for row in frozen_test:
            for m in row["messages"]:
                if m["role"] == "user":
                    g._register_text(m["content"], "test")
        print(f"test split dibekukan dari file lama ({len(frozen_test)} rows)")

    order = list(TRAIN_COUNTS)
    split_plan = [("train", TRAIN_COUNTS), ("validation", VAL_COUNTS)]
    if frozen_test is None:
        split_plan.append(("test", TEST_COUNTS))
    for split, counts in split_plan:
        for rtype in order:
            g.gen_type(rtype, split, counts[rtype])
        g.rng.shuffle(g.rows[split])
    if frozen_test is not None:
        g.rows["test"] = frozen_test

    self_check(g.rows)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for split, rows in g.rows.items():
        if split == "test" and frozen_test is not None:
            print(f"skip tulis {OUT_DIR / 'test.jsonl'} (dibekukan, {len(rows)} rows)")
            continue
        path = OUT_DIR / f"{split}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"wrote {path} ({len(rows)} rows)")

    st = stats(g.rows)
    (OUT_DIR / "stats.json").write_text(json.dumps(st, indent=2, ensure_ascii=False))
    print(json.dumps(st, indent=2, ensure_ascii=False))
    print("self-check OK")


if __name__ == "__main__":
    main()
