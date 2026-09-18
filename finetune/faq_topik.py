"""Dokumen FAQ untuk dataset v7 — bahan latihan MEMBACA konteks, bukan hafalan.

Chatbot menjawab pertanyaan toko dari FAQ hasil RAG, dan FAQ itu disunting
lewat Admin Site kapan saja. Kalau isi FAQ yang dilatih, model menghafal fakta
hari ini dan tetap mengulangnya sesudah FAQ diubah. Jadi yang dilatih adalah
keterampilannya: baca KONTEKS FAQ di pesan, jawab dari situ.

Caranya, setiap topik punya SLOT fakta (jam buka, batas bayar, persen DP, …)
yang diacak per baris. Dokumen dan jawaban acuan dirender dari slot yang sama,
jadi pertanyaan yang sama muncul dengan jawaban berbeda di baris berbeda —
satu-satunya cara menjawab benar adalah membaca konteksnya ("fakta tandingan").

Yang TIDAK diacak: cara kerja bot itu sendiri (ketik "batal", "status pesanan",
dsb.), karena itu perilaku kode, bukan isi FAQ.

TOPIK_TEST hanya muncul di split test: kalau model tetap benar di sana, ia
memang membaca konteks, bukan mengenali dokumen yang pernah dilihat.

FAQ_ASLI = 16 baris faq_items di VM (19 Sep 2026). Dipakai sebagian kecil saja,
sebagai contoh format dokumen yang benar-benar diterima model di produksi.
"""

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent


def _pilih(rng, *opsi):
    return rng.choice(opsi)


# ── Slot ──────────────────────────────────────────────────────────────────────
def s_jam(rng):
    hari, libur = _pilih(rng, ("Senin sampai Sabtu", "Hari Minggu kami libur."),
                         ("Senin sampai Jumat", "Sabtu dan Minggu kami libur."),
                         ("setiap hari", "Kami buka juga di akhir pekan."),
                         ("Selasa sampai Minggu", "Hari Senin kami libur."))
    return {"hari": hari, "libur": libur,
            "buka": _pilih(rng, "08.00", "09.00", "10.00"),
            "tutup": _pilih(rng, "17.00", "18.00", "19.00", "20.00", "21.00")}


def s_tahan(rng):
    return {"ruang": _pilih(rng, "1-2", "2-3", "3-4"),
            "dingin": _pilih(rng, "4-5", "5-7", "7-10", "10-14")}


def s_kirim(rng):
    kurir = _pilih(rng, "GoSend atau GrabExpress", "GoSend", "GrabExpress",
                   "Maxim atau GoSend")
    luar = _pilih(rng, ("Untuk luar kota, saat ini belum kami layani.", "belum melayani luar kota", "Out-of-town delivery isn't available yet."),
                  ("Pengiriman luar kota bisa lewat ekspedisi dengan ongkir ditanggung pembeli.",
                   "bisa kirim luar kota lewat ekspedisi dengan ongkir ditanggung pembeli", "Out-of-town delivery is possible via a shipping service, paid by the buyer."))
    return {"kurir": kurir, "luar": luar[0], "luar_ringkas": luar[1], "luar_en": luar[2]}


def s_bayar(rng):
    return {"metode": _pilih(rng, "QRIS atau transfer Virtual Account (VA) bank BCA",
                             "QRIS atau transfer Virtual Account (VA) bank BNI",
                             "QRIS atau transfer Virtual Account (VA) bank Mandiri",
                             "QRIS saja")}


def s_batas(rng):
    return {"menit": _pilih(rng, "15", "30", "45", "60")}


def s_dp(rng):
    return {"persen": _pilih(rng, "30", "50"),
            "lunas": _pilih(rng, "saat pesanan siap diambil", "sebelum pesanan dikirim",
                            "paling lambat H-1")}


def s_halal(rng):
    return _pilih(rng,
                  {"isi": "Ya, semua produk Toti Cakery dibuat dari bahan-bahan halal.",
                   "ringkas": "semua produk dibuat dari bahan-bahan halal", "en": "Yes, all our products are made with halal ingredients."},
                  {"isi": "Semua bahan yang kami pakai halal, tapi dapur kami belum memiliki sertifikat halal resmi.",
                   "ringkas": "bahan yang dipakai halal, tapi dapur belum memiliki sertifikat halal resmi", "en": "All our ingredients are halal, but our kitchen isn't officially halal-certified yet."},
                  {"isi": "Ya, produk kami sudah bersertifikat halal MUI.",
                   "ringkas": "produk sudah bersertifikat halal MUI", "en": "Yes, our products are halal-certified by MUI."})


def s_custom(rng):
    return {"h": _pilih(rng, "H-2", "H-3", "H-5", "H-7")}


def s_lokasi(rng):
    return {"area": _pilih(rng, "Batam Centre", "Nagoya", "Tiban", "Sekupang", "Batu Aji"),
            "jalan": _pilih(rng, "Jl. Raja Ali Haji", "Jl. Engku Putri", "Jl. Sudirman",
                            "Ruko Mitra Raya Blok B", "Komplek Tiban Indah")}


def s_samedy(rng):
    return _pilih(rng,
                  {"isi": "Bisa, pesanan reguler bisa diambil di hari yang sama selama dipesan sebelum pukul 12.00.",
                   "ringkas": "bisa di hari yang sama selama dipesan sebelum pukul 12.00", "en": "Yes, regular orders can be picked up the same day if ordered before 12.00."},
                  {"isi": "Belum bisa. Semua pesanan dibuat fresh, jadi minimal dipesan H-1.",
                   "ringkas": "belum bisa, minimal dipesan H-1", "en": "Not yet — everything is made fresh, so please order at least one day ahead."},
                  {"isi": "Bisa untuk produk yang stoknya tersedia, tapi kue tart minimal H-1.",
                   "ringkas": "bisa untuk produk yang stoknya tersedia, tapi kue tart minimal H-1", "en": "Yes for items in stock, but tart cakes need to be ordered at least one day ahead."})


def s_besar(rng):
    return {"batas": _pilih(rng, "20", "30", "50"), "h": _pilih(rng, "H-2", "H-3", "H-5")}


def s_alergen(rng):
    return _pilih(rng,
                  {"isi": "Produk kami mengandung telur, susu, dan gluten. Saat ini belum ada varian tanpa telur.",
                   "ringkas": "produk mengandung telur, susu, dan gluten, dan belum ada varian tanpa telur", "en": "Our products contain egg, milk, and gluten, and there is no egg-free option yet."},
                  {"isi": "Sebagian besar produk mengandung telur, susu, dan kacang. Varian tanpa telur tersedia untuk brownies.",
                   "ringkas": "sebagian besar produk mengandung telur, susu, dan kacang, tapi ada varian tanpa telur untuk brownies", "en": "Most products contain egg, milk, and nuts, but there is an egg-free brownie option."})


def s_website(rng):
    return _pilih(rng,
                  {"isi": "Bisa. Pemesanan juga tersedia lewat website Toti Cakery, dan statusnya tetap bisa ditanyakan lewat chat ini.",
                   "ringkas": "bisa, pemesanan juga tersedia lewat website Toti Cakery", "en": "Yes, you can also order on the Toti Cakery website."},
                  {"isi": "Untuk saat ini pemesanan hanya lewat chat WhatsApp ini. Website hanya menampilkan katalog.",
                   "ringkas": "pemesanan hanya lewat chat WhatsApp ini, website hanya menampilkan katalog", "en": "For now, orders are only taken through this WhatsApp chat; the website only shows the catalogue."})


def s_ongkir(rng):
    return _pilih(rng,
                  {"isi": "Ongkos kirim dibayar langsung oleh pembeli ke kurir ojek online.",
                   "ringkas": "ongkos kirim dibayar langsung oleh pembeli ke kurir", "en": "The delivery fee is paid by the buyer directly to the courier."},
                  {"isi": "Gratis ongkir untuk jarak di bawah 3 km dari toko. Di atas itu, ongkos kirim dibayar pembeli ke kurir.",
                   "ringkas": "gratis ongkir untuk jarak di bawah 3 km dari toko, di atas itu dibayar pembeli ke kurir", "en": "Delivery is free within 3 km of the store; beyond that the buyer pays the courier."},
                  {"isi": "Gratis ongkir untuk pembelian di atas dua kotak. Selain itu ongkos kirim ditanggung pembeli.",
                   "ringkas": "gratis ongkir untuk pembelian di atas dua kotak, selain itu ditanggung pembeli", "en": "Delivery is free for orders above two boxes; otherwise the buyer pays."})


def s_retur(rng):
    return {"jam": _pilih(rng, "1", "2", "3", "6")}


def s_kemasan(rng):
    return _pilih(rng,
                  {"isi": "Setiap kue dikemas dalam kotak dan bisa ditambah kartu ucapan gratis. Sampaikan isi ucapannya saat memesan.",
                   "ringkas": "kue dikemas dalam kotak dan bisa ditambah kartu ucapan gratis", "en": "Every cake comes in a box, and you can add a free greeting card."},
                  {"isi": "Kue dikemas dalam kotak mika. Kartu ucapan belum tersedia.",
                   "ringkas": "kue dikemas dalam kotak mika dan kartu ucapan belum tersedia", "en": "Cakes come in a clear plastic box; greeting cards aren't available yet."})


def s_kosong(_rng):
    return {}


# ── Topik ─────────────────────────────────────────────────────────────────────
# doc_q/doc_a = isi dokumen ("Q: …\nA: …", format runtime app/rag/faq_source.py).
# q_id/q_en = cara pelanggan bertanya; r_id/r_en = jawaban acuan dari slot yang
# sama. Semua string di-.format(**slot).
TOPIK = {
    "jam": {
        "slot": s_jam,
        "doc_q": "Jam berapa Toti Cakery buka?",
        "doc_a": "Toti Cakery buka {hari} pukul {buka} - {tutup} WIB. {libur}",
        "q_id": ["{greet}jam berapa toti cakery buka{part}?", "{greet}buka sampai jam berapa{part}?",
                 "{greet}jam operasionalnya gimana{part}?", "{greet}besok buka ga{part}?",
                 "{greet}jam segini masih buka{part}?", "{greet}tokonya tutup jam berapa{part}?"],
        "q_en": ["{greet}what are your opening hours?", "{greet}what time do you close?",
                 "{greet}are you open on weekends?"],
        "r_id": ["Toti Cakery buka {hari} pukul {buka} - {tutup} WIB ya kak. {libur} 😊",
                 "Kami buka {hari}, jam {buka} sampai {tutup} WIB. {libur}"],
        "r_en": ["We're open {hari_en}, {buka} - {tutup} WIB 😊"],
    },
    "tahan": {
        "slot": s_tahan,
        "doc_q": "Berapa lama daya tahan kue Toti Cakery?",
        "doc_a": ("Kue kami tahan {ruang} hari di suhu ruang, dan hingga {dingin} hari jika "
                  "disimpan di lemari es dalam wadah tertutup rapat. Kue paling enak dinikmati "
                  "di hari yang sama."),
        "q_id": ["{greet}kuenya tahan berapa lama{part}?", "{greet}kalau disimpan di kulkas awet berapa hari{part}?",
                 "{greet}aman ga disimpan sampai lusa{part}?", "{greet}daya tahan kuenya berapa hari{part}?"],
        "q_en": ["{greet}how long do the cakes last?", "{greet}how long can I keep it in the fridge?"],
        "r_id": ["Kue kami tahan {ruang} hari di suhu ruang, dan hingga {dingin} hari kalau disimpan di lemari es dalam wadah tertutup rapat ya kak 😊",
                 "Di suhu ruang tahan {ruang} hari, di lemari es hingga {dingin} hari. Paling enak dinikmati di hari yang sama kak 🙏"],
        "r_en": ["They last {ruang} days at room temperature and up to {dingin} days in the fridge in a sealed container 😊"],
    },
    "kirim": {
        "slot": s_kirim,
        "doc_q": "Bagaimana metode pengiriman di Toti Cakery? Apakah bisa kirim ke luar kota?",
        "doc_a": ("Ada dua metode: ambil sendiri (pickup) di toko, atau delivery dalam kota. "
                  "Untuk delivery, kurirnya dipesan sendiri oleh pelanggan lewat {kurir} dari "
                  "alamat toko kami. {luar}"),
        "q_id": ["{greet}bisa dikirim ke rumah ga{part}?", "{greet}pengirimannya gimana{part}?",
                 "{greet}bisa kirim ke luar kota{part}?", "{greet}delivery pakai apa{part}?",
                 "{greet}kurirnya dari toko atau pesan sendiri{part}?"],
        "q_en": ["{greet}how does delivery work?", "{greet}do you deliver outside the city?"],
        "r_id": ["Bisa ambil sendiri (pickup) di toko atau delivery dalam kota kak. Untuk delivery, kurirnya dipesan sendiri lewat {kurir} dari alamat toko kami. {luar}",
                 "Ada dua metode kak: pickup di toko, atau delivery dalam kota dengan kurir {kurir} yang dipesan sendiri oleh pelanggan. {luar}"],
        "r_en": ["You can pick up at the store or get delivery within the city — you book the courier yourself via {kurir} from our store address. {luar_en} 🙏"],
    },
    "bayar": {
        "slot": s_bayar,
        "doc_q": "Metode pembayaran apa saja yang tersedia?",
        "doc_a": ("Pembayaran dilakukan secara non-tunai melalui {metode}. Setelah pesanan "
                  "dikonfirmasi, kami kirimkan kode pembayaran beserta nominal dan batas waktunya."),
        "q_id": ["{greet}bayarnya bisa pakai apa aja{part}?", "{greet}bisa bayar cash ga{part}?",
                 "{greet}bisa COD ga{part}?", "{greet}terima transfer bank apa{part}?",
                 "{greet}pembayarannya lewat apa{part}?"],
        "q_en": ["{greet}what payment methods do you accept?", "{greet}can I pay cash?"],
        "r_id": ["Pembayarannya non-tunai ya kak, melalui {metode}. Setelah pesanan dikonfirmasi, kode pembayarannya kami kirimkan 😊",
                 "Bisa lewat {metode} kak — semuanya non-tunai 🙏"],
        "r_en": ["Payment is cashless, via {metode} 😊"],
    },
    "batas": {
        "slot": s_batas,
        "doc_q": "Berapa lama batas waktu pembayaran setelah pesanan dibuat?",
        "doc_a": ("Batas waktunya {menit} menit sejak tagihan terbit. Lewat dari itu, pesanan "
                  "dibatalkan otomatis oleh sistem, dan kamu bisa memesan ulang kapan saja."),
        "q_id": ["{greet}batas waktu bayarnya berapa lama{part}?", "{greet}kalau telat bayar gimana{part}?",
                 "{greet}bayarnya harus kapan{part}?", "{greet}tagihannya berlaku berapa menit{part}?"],
        "q_en": ["{greet}how long do I have to pay?", "{greet}what happens if I pay late?"],
        "r_id": ["Batas waktunya {menit} menit sejak tagihan terbit kak. Lewat dari itu pesanan dibatalkan otomatis oleh sistem, tapi kamu bisa memesan ulang kapan saja 😊",
                 "Tagihan berlaku {menit} menit sejak terbit ya kak. Kalau lewat, pesanan dibatalkan otomatis dan bisa dipesan ulang 🙏"],
        "r_en": ["You have {menit} minutes from when the invoice is issued. After that the order is cancelled automatically, and you can order again anytime 😊"],
    },
    "dp": {
        "slot": s_dp,
        "doc_q": "Apakah bisa bayar DP dulu?",
        "doc_a": ("Bisa. Kamu bisa membayar DP {persen}% dari total pesanan lebih dulu supaya "
                  "pesanan kami proses, dan sisanya dilunasi {lunas}."),
        "q_id": ["{greet}bisa DP dulu ga{part}?", "{greet}DP nya berapa persen{part}?",
                 "{greet}sisanya dilunasi kapan{part}?", "{greet}boleh bayar setengah dulu{part}?"],
        "q_en": ["{greet}can I pay a deposit first?", "{greet}how much is the deposit?"],
        "r_id": ["Bisa kak, DP {persen}% dari total pesanan dulu, lalu sisanya dilunasi {lunas} 😊",
                 "Boleh bayar DP {persen}% dulu kak supaya pesanan kami proses, sisanya dilunasi {lunas} 🙏"],
        "r_en": ["Yes — you can pay a {persen}% deposit first and settle the rest {lunas_en} 😊"],
    },
    "halal": {
        "slot": s_halal,
        "doc_q": "Apakah kue Toti Cakery halal?",
        "doc_a": "{isi}",
        "q_id": ["{greet}kuenya halal ga{part}?", "{greet}udah ada sertifikat halal{part}?",
                 "{greet}bahannya halal kan{part}?"],
        "q_en": ["{greet}are your cakes halal?"],
        "r_id": ["{isi} 😊"],
        "r_en": ["{en} 😊"],
    },
    "custom": {
        "slot": s_custom,
        "doc_q": "Apakah Toti Cakery menerima pesanan kue custom?",
        "doc_a": ("Ya, kami menerima kue custom seperti kue ulang tahun dengan desain, tulisan, "
                  "dan tema tertentu. Pesanan custom minimal {h} dan diteruskan ke admin untuk "
                  "diskusi detailnya."),
        "q_id": ["{greet}terima pesanan kue custom ga{part}?", "{greet}kalau kue custom harus pesan kapan{part}?",
                 "{greet}bisa request tema kue ga{part}?"],
        "q_en": ["{greet}do you take custom cake orders?", "{greet}how early should I order a custom cake?"],
        "r_id": ["Bisa kak, kami menerima kue custom dengan desain, tulisan, dan tema tertentu. Pesanannya minimal {h} ya, dan detailnya didiskusikan dengan admin 😊"],
        "r_en": ["Yes, we take custom cakes (design, writing, theme). Please order at least {h} ahead; the details are discussed with our admin 😊"],
    },
    "lokasi": {
        "slot": s_lokasi,
        "doc_q": "Di mana lokasi Toti Cakery?",
        "doc_a": "Toko kami berada di {jalan}, {area}, Batam. Pesanan bisa diambil langsung di sana.",
        "q_id": ["{greet}tokonya di mana{part}?", "{greet}alamat tokonya di mana ya{part}?",
                 "{greet}kalau mau ambil sendiri ke mana{part}?"],
        "q_en": ["{greet}where is your store?", "{greet}what's your address?"],
        "r_id": ["Toko kami di {jalan}, {area}, Batam kak. Pesanan bisa diambil langsung di sana 😊"],
        "r_en": ["We're at {jalan}, {area}, Batam — you can pick up your order there 😊"],
    },
    "samedy": {
        "slot": s_samedy,
        "doc_q": "Apakah bisa pesan untuk diambil di hari yang sama?",
        "doc_a": "{isi}",
        "q_id": ["{greet}bisa pesan buat hari ini juga{part}?", "{greet}pesan dadakan bisa ga{part}?",
                 "{greet}kalau pesan sekarang bisa jadi hari ini{part}?"],
        "q_en": ["{greet}can I order for today?", "{greet}is same-day pickup possible?"],
        "r_id": ["{isi} 😊"],
        "r_en": ["{en} 😊"],
    },
    "besar": {
        "slot": s_besar,
        "doc_q": "Bagaimana kalau pesan dalam jumlah besar?",
        "doc_a": ("Untuk pesanan di atas {batas} pcs, pemesanan minimal {h} supaya produksinya "
                  "bisa dijadwalkan."),
        "q_id": ["{greet}kalau pesan banyak harus dari kapan{part}?", "{greet}pesanan jumlah besar minimal H berapa{part}?",
                 "{greet}buat acara kantor pesan banyak, harus jauh hari ga{part}?"],
        "q_en": ["{greet}how early should I place a big order?"],
        "r_id": ["Untuk pesanan di atas {batas} pcs, pemesanan minimal {h} ya kak, supaya produksinya bisa dijadwalkan 😊"],
        "r_en": ["For orders above {batas} pieces, please order at least {h} ahead so we can schedule production 😊"],
    },
    "alergen": {
        "slot": s_alergen,
        "doc_q": "Apakah produk mengandung alergen?",
        "doc_a": "{isi}",
        "q_id": ["{greet}ada yang tanpa telur ga{part}?", "{greet}kuenya pakai kacang ga{part}?",
                 "{greet}anakku alergi susu, aman ga{part}?"],
        "q_en": ["{greet}do you have egg-free cakes?", "{greet}do your cakes contain nuts?"],
        "r_id": ["{isi} 🙏"],
        "r_en": ["{en} 🙏"],
    },
    "website": {
        "slot": s_website,
        "doc_q": "Apakah bisa memesan lewat website?",
        "doc_a": "{isi}",
        "q_id": ["{greet}bisa pesan lewat website ga{part}?", "{greet}ada web buat order{part}?"],
        "q_en": ["{greet}can I order on your website?"],
        "r_id": ["{isi} 😊"],
        "r_en": ["{en} 😊"],
    },
    "ongkir": {
        "slot": s_ongkir,
        "doc_q": "Siapa yang menanggung ongkos kirim?",
        "doc_a": "{isi}",
        "q_id": ["{greet}ongkirnya siapa yang bayar{part}?", "{greet}ada gratis ongkir ga{part}?",
                 "{greet}ongkos kirim ditanggung toko{part}?"],
        "q_en": ["{greet}who pays for delivery?", "{greet}is delivery free?"],
        "r_id": ["{isi} 😊"],
        "r_en": ["{en} 😊"],
    },
}

# Hanya di split test — lihat docstring modul.
TOPIK_TEST = {
    "retur": {
        "slot": s_retur,
        "doc_q": "Bagaimana kalau kue yang diterima rusak?",
        "doc_a": ("Sampaikan keluhan beserta foto kuenya paling lambat {jam} jam setelah "
                  "pesanan diterima supaya bisa kami tindak lanjuti."),
        "q_id": ["{greet}kalau kuenya rusak pas sampai, batas lapornya kapan{part}?",
                 "{greet}komplain kue rusak paling lambat kapan{part}?"],
        "q_en": ["{greet}how long do I have to report a damaged cake?"],
        "r_id": ["Sampaikan keluhan beserta foto kuenya paling lambat {jam} jam setelah pesanan diterima ya kak, supaya bisa kami tindak lanjuti 🙏"],
        "r_en": ["Please report it with a photo within {jam} hours of receiving the order so we can follow up 🙏"],
    },
    "kemasan": {
        "slot": s_kemasan,
        "doc_q": "Bagaimana kemasan kuenya?",
        "doc_a": "{isi}",
        "q_id": ["{greet}kuenya dikemas pakai apa{part}?", "{greet}bisa tambah kartu ucapan ga{part}?"],
        "q_en": ["{greet}can I add a greeting card?"],
        "r_id": ["{isi} 😊"],
        "r_en": ["{en} 😊"],
    },
}

_HARI_EN = {"Senin sampai Sabtu": "Monday to Saturday", "Senin sampai Jumat": "Monday to Friday",
            "setiap hari": "every day", "Selasa sampai Minggu": "Tuesday to Sunday"}
_LUNAS_EN = {"saat pesanan siap diambil": "when the order is ready for pickup",
             "sebelum pesanan dikirim": "before the order is delivered",
             "paling lambat H-1": "no later than one day before"}


def render_topik(topik: dict, rng) -> tuple[str, dict]:
    """(dokumen 'Q: …\\nA: …', slot) — slot dipakai lagi untuk jawaban acuan."""
    slot = dict(topik["slot"](rng))
    if "hari" in slot:
        slot["hari_en"] = _HARI_EN[slot["hari"]]
    if "lunas" in slot:
        slot["lunas_en"] = _LUNAS_EN[slot["lunas"]]
    doc = f"Q: {topik['doc_q']}\nA: {topik['doc_a'].format(**slot)}"
    return doc, slot


def faq_asli() -> list[str]:
    data = json.loads((_DIR / "faq_asli_vm.json").read_text(encoding="utf-8"))
    return [f"Q: {r['pertanyaan']}\nA: {r['jawaban']}" for r in data["baris"]]
