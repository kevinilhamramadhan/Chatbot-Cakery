"""Bahasa balasan tetap: satu templat, dua bahasa.

Model yang dipakai sudah dilatih dua bahasa, jadi jawaban bebas darinya sudah
ikut bahasa pelanggan dengan sendirinya. Yang TIDAK ikut adalah balasan tetap —
langkah checkout, kabar pembayaran, permintaan maaf, teks pembatalan — karena
semuanya ditulis tangan di dalam kode. Akibatnya pelanggan yang menulis bahasa
Inggris dijawab setengah Inggris setengah Indonesia dalam satu percakapan.

Modul ini menyatukan seluruh balasan tetap itu di satu tempat, masing-masing
dengan versi `id` dan `en`, lalu dipilih dari bahasa yang dipakai pelanggan.

Bahasa disimpan di sesi, bukan ditebak ulang tiap giliran: kabar proaktif
(pembayaran masuk, pesanan siap, dana sudah ditransfer) dikirim saat tidak ada
pesan masuk sama sekali, jadi tidak ada yang bisa dideteksi pada saat itu.
"""

import logging
import re

logger = logging.getLogger(__name__)

ID = "id"
EN = "en"
DEFAULT = ID

# Kata fungsi, bukan kata benda: nama kue, nama orang, dan alamat sama saja di
# kedua bahasa, jadi yang membedakan hanya kata perangkai kalimatnya.
# Sengaja TIDAK memuat kata domain yang dipakai apa adanya oleh pelanggan
# Indonesia juga: "delivery", "pickup", "order", "cake", "payment", "cancel",
# "yes", "no", "ok", "thanks". Terukur: satu pesan "delivery" di tengah
# percakapan Indonesia membalik seluruh sisa percakapan jadi bahasa Inggris.
_KATA_EN = {
    "the", "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "would", "should", "please", "you", "your", "yours",
    "i", "im", "ive", "my", "me", "we", "our", "want", "need", "have", "has",
    "how", "what", "when", "where", "which", "who", "why", "there", "here",
    "and", "or", "but", "with", "without", "for", "from", "this", "that",
    "hello", "hey", "still", "already", "again", "much",
    "many", "any", "some", "about", "sorry", "help", "buy", "much",
}
_KATA_ID = {
    "yang", "dan", "atau", "tapi", "dengan", "untuk", "dari", "ini", "itu",
    "saya", "aku", "kamu", "kak", "mau", "ingin", "bisa", "boleh", "tolong",
    "makasih", "terima", "kasih", "berapa", "gimana", "bagaimana", "apa",
    "kapan", "dimana", "kenapa", "mengapa", "ada", "gak", "ga", "nggak",
    "tidak", "belum", "sudah", "udah", "ya", "iya", "pesan", "pesanan", "kue",
    "harga", "bayar", "kirim", "antar", "ambil", "batal", "halo", "hai",
    "juga", "aja", "saja", "dong", "deh", "nih", "sih", "lagi", "masih",
}

_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)

# Berapa kata pencocok yang dibutuhkan sebelum bahasa percakapan dipindah.
_MIN_BUKTI = 2


def deteksi(teks: str) -> str | None:
    """Bahasa pesan ini, atau None kalau tidak cukup bukti.

    Sengaja mengembalikan None untuk pesan yang tidak menentukan ("ok", "1",
    "brownies 2"): menebak dari kalimat sependek itu akan membuat bahasa
    percakapan berganti-ganti di tengah jalan. Pemanggilnya menyimpan bahasa
    terakhir yang jelas, jadi giliran yang ambigu mengikuti yang sebelumnya.
    """
    kata = _PUNCT.sub(" ", (teks or "").lower()).split()
    if not kata:
        return None
    skor_en = sum(1 for k in kata if k in _KATA_EN)
    skor_id = sum(1 for k in kata if k in _KATA_ID)
    if skor_en == skor_id:
        return None
    menang = max(skor_en, skor_id)
    # Satu kata pencocok saja tidak cukup untuk memindah bahasa seluruh
    # percakapan: jawaban sependek "send" atau "ya" terlalu sering muncul di
    # kedua bahasa. Lebih baik ikut bahasa sebelumnya daripada salah pindah.
    if menang < _MIN_BUKTI:
        return None
    return EN if skor_en > skor_id else ID


def normalkan(lang: str | None) -> str:
    return EN if str(lang or "").lower().startswith("en") else ID


# ── Templat ───────────────────────────────────────────────────────────────────
# Satu kunci = satu balasan. Versi `en` bukan terjemahan harfiah: nada ramah
# yang sama, tapi ditulis sebagai kalimat Inggris yang wajar.
_TEMPLAT: dict[str, dict[str, str]] = {
    # ── Langkah checkout ─────────────────────────────────────────────────────
    "minta_nama": {
        ID: ("Siap! Untuk memproses pesanan, boleh aku minta *nama* kamu dulu?\n"
             "(ketik *batal* kalau berubah pikiran)"),
        EN: ("Great! To process your order, may I have your *name* first?\n"
             "(type *cancel* if you change your mind)"),
    },
    "nama_tidak_valid": {
        ID: "Namanya sepertinya kurang tepat. Boleh ketik nama lengkapmu?",
        EN: "That doesn't look like a name. Could you type your full name?",
    },
    "minta_alamat": {
        ID: "Halo {nama}! Sekarang, boleh minta *alamat*-mu?",
        EN: "Hi {nama}! Now, could I get your *address*?",
    },
    "alamat_tidak_valid": {
        ID: ("Alamatnya belum cukup jelas buat kurir. Boleh ketik alamat lengkapnya "
             "— nama jalan, nomor rumah, dan patokan kalau ada?"),
        EN: ("That address isn't clear enough for a courier. Could you type the full "
             "address — street name, house number, and a landmark if there is one?"),
    },
    # Pertanyaan metode pengiriman. Toko TIDAK punya kurir sendiri: yang disebut
    # "delivery" di sini artinya pelanggan memesan ojek online sendiri dari
    # alamat toko. Kalimatnya menyebutkan itu di depan, bukan nanti waktu
    # pesanannya sudah siap — pelanggan yang baru tahu di akhir sudah terlanjur
    # mengira ongkir ditanggung dan kurirnya diurus toko.
    "tanya_pengiriman": {
        ID: ("Pesanannya mau *diambil sendiri (pickup)* di toko, atau *dikirim*?\n\n"
             "Catatan: kami belum punya layanan antar sendiri. Kalau pilih *dikirim*, "
             "kuenya dijemput kurir ojek online (GoSend/GrabExpress) yang kamu pesan "
             "sendiri dari alamat toko, dan ongkirnya dibayar langsung ke kurir."),
        EN: ("Would you like to *pick up (pickup)* at the store, or have it *sent*?\n\n"
             "Note: we don't have our own delivery service. If you choose *sent*, the "
             "cake is collected by an online courier (GoSend/GrabExpress) that you "
             "book yourself from our store address, and you pay the courier directly."),
    },
    "pengiriman_tidak_jelas": {
        ID: ("Ketik *pickup* (ambil sendiri) atau *kirim* (kamu pesan kurir sendiri) ya."),
        EN: ("Please type *pickup* (collect it yourself) or *send* (you book the "
             "courier yourself)."),
    },
    # Dikirim setelah pelanggan memilih "kirim", supaya keputusan itu dikonfirmasi
    # dengan konsekuensinya, bukan lewat begitu saja.
    "konfirmasi_kirim_sendiri": {
        ID: ("Oke, dikirim ya. Nanti pas kuenya siap, aku kirimkan nama dan alamat "
             "toko — tinggal kamu salin ke aplikasi GoSend/GrabExpress untuk memesan "
             "kurirnya 🙏"),
        EN: ("Got it, sent it is. Once the cake is ready I'll send you our store name "
             "and address — just copy it into GoSend/GrabExpress to book the "
             "courier 🙏"),
    },
    "tanya_jenis_bayar": {
        ID: ("Mau bayar *penuh* atau *DP 50%*? Ketik salah satu ya.\n"
             "(DP 50% = bayar separuh dulu sekarang)"),
        EN: ("Would you like to pay in *full* or a *50% deposit*? Type one.\n"
             "(50% deposit = pay half now)"),
    },
    "lanjut_bayar": {
        ID: "Lanjut ke pembayaran ya...",
        EN: "Moving on to payment...",
    },
    "tanya_metode_bayar": {
        ID: ("Metode pembayarannya mau lewat apa?\n"
             "• Ketik *VA* — transfer bank via Virtual Account\n"
             "• Ketik *QRIS* — scan kode QR (GoPay/OVO/Dana/mobile banking)"),
        EN: ("How would you like to pay?\n"
             "• Type *VA* — bank transfer via Virtual Account\n"
             "• Type *QRIS* — scan a QR code (GoPay/OVO/Dana/mobile banking)"),
    },
    # ── Membuat pesanan & tagihan (checkout.finalize_order) ─────────────────
    "pesanan_dibuat": {
        ID: ("Pesanan kamu sudah dibuat ✅\nNo. Invoice: *{invoice}*\n\n"
             "{label} yang harus dibayar: *{jumlah}*{total}\n\n{cara_bayar}\n\n"
             "Batas waktu pembayaran: {menit} menit. Pembayaran akan terdeteksi "
             "otomatis. Ketik *batal* kalau ingin membatalkan."),
        EN: ("Your order has been created ✅\nInvoice no.: *{invoice}*\n\n"
             "{label} due: *{jumlah}*{total}\n\n{cara_bayar}\n\n"
             "Payment deadline: {menit} minutes. Your payment is detected "
             "automatically. Type *cancel* if you'd like to cancel."),
    },
    "label_bayar_penuh": {ID: "Pembayaran penuh", EN: "Full payment"},
    "label_dp": {ID: "DP 50%", EN: "50% deposit"},
    "total_pesanan_dp": {ID: " (total pesanan {total})", EN: " (order total {total})"},
    "harga_belum_pasti": {
        ID: "Maaf, harga tidak bisa dipastikan sekarang. Coba ulangi sebentar lagi ya 🙏",
        EN: "Sorry, I can't confirm the prices right now. Please try again in a moment 🙏",
    },
    "semua_item_habis": {
        ID: "Maaf, semua item di pesananmu sudah tidak tersedia. Mau lihat menu terbaru?",
        EN: ("Sorry, none of the items in your order are available anymore. "
             "Want to see the latest menu?"),
    },
    "item_dikeluarkan": {
        ID: "{nama} sudah tidak tersedia dan aku keluarkan dari pesanan",
        EN: "{nama} is no longer available, so I've removed it from your order",
    },
    "harga_berubah": {
        ID: "harga {nama} berubah dari {lama} jadi {baru}",
        EN: "the price of {nama} changed from {lama} to {baru}",
    },
    "ada_update_harga": {
        ID: ("Sebelum lanjut, ada update: {catatan}.\n\nTotal sekarang: {total}\n"
             "Ketik *sudah sesuai* kalau setuju, atau *batal* untuk membatalkan ya 🙏"),
        EN: ("Before we continue, there's an update: {catatan}.\n\nNew total: {total}\n"
             "Type *confirm* if that's fine, or *cancel* to cancel 🙏"),
    },
    "masih_ada_tagihan": {
        ID: ("Kamu masih punya tagihan yang belum dibayar. Selesaikan dulu "
             "pembayaran itu, atau ketik *batal* untuk membatalkannya, baru "
             "kita buat pesanan baru ya 🙏"),
        EN: ("You still have an unpaid invoice. Please finish that payment first, "
             "or type *cancel* to cancel it, and then we can make a new order 🙏"),
    },
    # {alasan} datang dari backend dan selalu berbahasa Indonesia.
    "pesanan_ditolak": {
        ID: ("Maaf, {alasan}\n\nPesanannya belum jadi dibuat ya. Mau pilih kue yang "
             "lain? Ketik *menu* untuk lihat daftarnya 🙏"),
        EN: ("Sorry, we couldn't create your order: {alasan}\n\nWould you like to "
             "pick a different cake? Type *menu* to see the list 🙏"),
    },
    "pesanan_gagal": {
        ID: "Maaf, pembuatan pesanan gagal. Coba ulangi sebentar lagi ya 🙏",
        EN: "Sorry, I couldn't create your order. Please try again in a moment 🙏",
    },
    "tagihan_gagal": {
        ID: "Maaf, pembuatan tagihan gagal. Coba ulangi sebentar lagi ya 🙏",
        EN: "Sorry, I couldn't create the invoice. Please try again in a moment 🙏",
    },
    "tagihan_tanpa_cara_bayar": {
        ID: ("Maaf, tagihannya gagal diterbitkan jadi pesanannya belum kubuat. "
             "Coba ulangi sebentar lagi ya 🙏"),
        EN: ("Sorry, the invoice couldn't be issued, so I haven't created the order. "
             "Please try again in a moment 🙏"),
    },
    # ── Konfirmasi keranjang & pembatalan ────────────────────────────────────
    "pesanan_dibatalkan": {
        ID: "Oke, pesanan dibatalkan ya. Ada lagi yang bisa kubantu? 😊",
        EN: "Alright, the order is cancelled. Anything else I can help with? 😊",
    },
    "pesanan_dibatalkan_singkat": {
        ID: "Oke, pesanan dibatalkan ya. 😊",
        EN: "Alright, the order is cancelled. 😊",
    },
    "keranjang_kosong": {
        ID: "Keranjangmu kosong. Mau lihat menu dulu?",
        EN: "Your cart is empty. Want to see the menu first?",
    },
    "ulangi_konfirmasi_keranjang": {
        ID: ("Kalau pesanannya sudah pas, ketik *sudah sesuai* ya — "
             "atau *batal* kalau berubah pikiran."),
        EN: ("If the order looks right, type *confirm* — or *cancel* if you've "
             "changed your mind."),
    },
    "kembali_minta_nama": {
        ID: "Balik ke pesanan ya — boleh aku minta *nama* kamu?",
        EN: "Back to your order — may I have your *name*?",
    },
    "kembali_minta_alamat": {
        ID: "Lanjut ya — boleh minta *alamat* lengkapmu?",
        EN: "Let's continue — could I get your full *address*?",
    },
    "ada_lagi": {
        ID: "Ada lagi yang bisa kubantu? 😊",
        EN: "Anything else I can help you with? 😊",
    },
    # ── Pembatalan pesanan berbayar ──────────────────────────────────────────
    "ulangi_konfirmasi_batal": {
        ID: "Balik ke tadi ya — pesanannya jadi dibatalkan? Ketik *ya* atau *tidak* 🙏",
        EN: "Back to that — do you still want to cancel the order? Type *yes* or *no* 🙏",
    },
    "batal_dibatalkan": {
        ID: "Oke, pesanannya tetap kami proses ya 😊",
        EN: "Alright, we'll keep processing your order 😊",
    },
    # ── cancel_order & proses_pembatalan ─────────────────────────────────────
    "draft_dikosongkan": {
        ID: "Oke, draft pesanan dikosongkan. Ada lagi yang bisa kubantu?",
        EN: "Alright, I've cleared your draft order. Anything else I can help with?",
    },
    "bukan_niat_batal": {
        ID: ("Ada yang bisa kubantu? Ketik *menu* untuk lihat daftar kue, atau *batal* "
             "kalau memang mau membatalkan pesanan 😊"),
        EN: ("How can I help? Type *menu* to see our cakes, or *cancel* if you do want "
             "to cancel your order 😊"),
    },
    "konfirmasi_batal": {
        ID: ("Pesanan *{label}* mau dibatalkan ya?\n\n"
             "Ketik *ya* untuk membatalkan, atau *tidak* kalau pesanannya diteruskan 🙏"),
        EN: ("Do you want to cancel order *{label}*?\n\n"
             "Type *yes* to cancel, or *no* to keep the order 🙏"),
    },
    "konfirmasi_batal_berbayar": {
        ID: ("Pesanan *{label}* sudah dibayar, jadi pembatalannya sekalian dengan "
             "pengembalian dana. {cara}\n\n"
             "Mau aku proses sekarang? Ketik *ya* untuk membatalkan dan mengembalikan "
             "dananya, atau *tidak* kalau pesanannya diteruskan saja 🙏"),
        EN: ("Order *{label}* has already been paid, so cancelling it also means a "
             "refund. {cara}\n\n"
             "Shall I process it now? Type *yes* to cancel and get your money back, or "
             "*no* to keep the order 🙏"),
    },
    "cara_refund_transfer": {
        ID: ("Dananya dikembalikan tim kami lewat transfer, jadi butuh waktu "
             "beberapa hari kerja."),
        EN: "Our team returns the money by bank transfer, so it takes a few working days.",
    },
    "cara_refund_otomatis": {
        ID: "Dananya kembali otomatis ke aplikasi yang kamu pakai buat bayar.",
        EN: "The money goes back automatically to the app you paid with.",
    },
    "sudah_dikerjakan": {
        ID: ("Pesanan ini sudah dalam tahap pengerjaan sehingga tidak bisa dibatalkan.\n"
             "Jika ada keluhan mohon kirim ke alamat email kami di {alamat}\n"
             "Terima Kasih"),
        EN: ("This order is already being made, so it can no longer be cancelled.\n"
             "If you have a complaint, please email us at {alamat}\n"
             "Thank you"),
    },
    "kontak_resmi": {ID: "kontak resmi Toti Cakery", EN: "Toti Cakery's official contact"},
    "tidak_ada_yang_dibatalkan": {
        ID: "Tidak ada pesanan aktif yang perlu dibatalkan 😊",
        EN: "There's no active order to cancel 😊",
    },
    "batal_perlu_tim": {
        ID: "Pembatalannya perlu diproses tim kami dulu supaya dananya bisa dikembalikan.",
        EN: "Our team needs to process this cancellation first so the money can be returned.",
    },
    "tindak_lanjut_email": {
        ID: ("\n\nKirim nomor pesananmu ke {email} ya, nanti tim kami yang "
             "menindaklanjuti 🙏"),
        EN: "\n\nPlease send your order number to {email} and our team will follow up 🙏",
    },
    "tindak_lanjut_umum": {
        ID: "\n\nSampaikan nomor pesananmu ke kontak resmi Toti Cakery ya 🙏",
        EN: "\n\nPlease send your order number to Toti Cakery's official contact 🙏",
    },
    "batal_belum_bayar": {
        ID: "Pesanan kamu sudah dibatalkan. Terima kasih 🙏",
        EN: "Your order has been cancelled. Thank you 🙏",
    },
    "batal_refund_manual": {
        ID: ("Pesanan kamu sudah dibatalkan ✅\n\n"
             "Pengembalian dananya diproses tim kami lewat transfer, jadi mohon "
             "ditunggu beberapa hari kerja. {tutup}"),
        EN: ("Your order has been cancelled ✅\n\n"
             "Our team is processing your refund by bank transfer, so please allow a "
             "few working days. {tutup}"),
    },
    "tutup_refund_email": {
        ID: "Kalau dalam 3 hari kerja belum masuk, kabari kami di {email} ya 🙏",
        EN: "If it hasn't arrived within 3 working days, let us know at {email} 🙏",
    },
    "tutup_refund_umum": {
        ID: "Kalau dalam 3 hari kerja belum masuk, kabari kami ya 🙏",
        EN: "If it hasn't arrived within 3 working days, please let us know 🙏",
    },
    "batal_refund_otomatis": {
        ID: ("Pesanan kamu sudah dibatalkan dan pengembalian dananya diproses ✅\n\n"
             "Dananya kembali otomatis ke aplikasi yang kamu pakai buat bayar, biasanya "
             "dalam beberapa menit sampai beberapa jam ya 🙏"),
        EN: ("Your order has been cancelled and your refund is being processed ✅\n\n"
             "The money goes back automatically to the app you paid with, usually "
             "within a few minutes to a few hours 🙏"),
    },
    # ── Eskalasi ke admin ────────────────────────────────────────────────────
    "tawaran_admin": {
        ID: ("Sepertinya ini lebih enak ditangani admin kami langsung. "
             "Mau aku sambungkan ke admin? Ketik *ya* untuk kusambungkan, atau lanjut "
             "tanya ke aku kalau masih ada yang bisa kubantu 😊"),
        EN: ("This is probably better handled by our admin directly. Shall I connect "
             "you? Type *yes* and I'll pass you over, or keep asking me if there's "
             "anything else I can help with 😊"),
    },
    "diteruskan_ke_admin": {
        ID: ("Oke, permintaanmu sudah aku teruskan ke admin kami ya. Mohon tunggu, admin "
             "akan menghubungimu langsung lewat chat ini. 🙏"),
        EN: ("Alright, I've passed your request to our admin. Please hold on — they'll "
             "contact you directly in this chat. 🙏"),
    },
    "admin_tidak_tersedia": {
        ID: ("Maaf, aku belum bisa menyambungkanmu ke admin sekarang — nomor adminnya "
             "sedang tidak bisa dihubungi. Coba beberapa saat lagi ya 🙏"),
        EN: ("Sorry, I can't connect you to an admin right now — their number isn't "
             "reachable. Please try again in a little while 🙏"),
    },
    # ── Keluhan ──────────────────────────────────────────────────────────────
    "maaf_keluhan": {
        ID: ("Mohon maaf sekali atas ketidaknyamanan yang dialami. Masukanmu akan "
             "menjadi bahan perbaikan kami ke depannya."),
        EN: ("We're very sorry for the inconvenience you experienced. Your feedback "
             "will help us do better going forward."),
    },
    "maaf_keluhan_email": {
        ID: ("Jika ada keluhan lebih lanjut, anda dapat mengirimkannya ke alamat "
             "email kami di {email} agar tim kami dapat merespon dengan lebih akurat."),
        EN: ("If you have any further complaints, you can send them to our email at "
             "{email} so our team can respond more precisely."),
    },
    "maaf_keluhan_penutup": {
        ID: "Terima kasih",
        EN: "Thank you",
    },
    # ── Kabar proaktif ───────────────────────────────────────────────────────
    "pesanan_siap": {
        ID: "Kabar baik! Pesananmu *{label}* sudah *siap* 🎉\n",
        EN: "Good news! Your order *{label}* is *ready* 🎉\n",
    },
    "pesanan_siap_kirim": {
        ID: ("\nUntuk pengiriman, silakan pesan kurir (GoSend/GrabExpress) sendiri ke "
             "alamat toko berikut:\n*{nama_toko}*\n{alamat_toko}\n"
             "(salin alamat di atas ke aplikasi ojol ya)"),
        EN: ("\nTo have it sent, please book a courier (GoSend/GrabExpress) yourself "
             "to this store address:\n*{nama_toko}*\n{alamat_toko}\n"
             "(copy the address above into your ride-hailing app)"),
    },
    "pesanan_siap_ambil": {
        ID: "\nSilakan diambil di {nama_toko}, {alamat_toko}.",
        EN: "\nPlease collect it at {nama_toko}, {alamat_toko}.",
    },
    # ── Keluaran tool ────────────────────────────────────────────────────────
    "ringkasan_keranjang_judul": {
        ID: "Ringkasan pesananmu sejauh ini:",
        EN: "Your order so far:",
    },
    "ringkasan_keranjang_total": {
        ID: "\nTotal: {total}",
        EN: "\nTotal: {total}",
    },
    "keranjang_kosong_tool": {
        ID: ("Keranjangmu masih kosong. Ketik *menu* untuk lihat daftar kue, "
             "atau sebutkan kue dan jumlahnya ya \U0001F60A"),
        EN: ("Your cart is still empty. Type *menu* to see our cake list, or just "
             "tell me the cake and how many you'd like \U0001F60A"),
    },
    "keranjang_ajak_konfirmasi": {
        ID: "\n\nKetik *sudah sesuai* kalau sudah pas ya \U0001F60A",
        EN: "\n\nType *confirm* when it looks right \U0001F60A",
    },
    "keranjang_tanya_tambah": {
        ID: ("\n\nSudah sesuai semua, atau mau nambah lagi? "
             "Ketik *sudah sesuai* untuk lanjut ya \U0001F60A"),
        EN: ("\n\nIs that everything, or would you like to add more? "
             "Type *confirm* to continue \U0001F60A"),
    },
    "status_pesanan_gagal": {
        ID: "Maaf, status pesanan lagi tidak bisa diambil. Coba lagi sebentar ya \U0001F64F",
        EN: "Sorry, I can't fetch your order status right now. Please try again shortly \U0001F64F",
    },
    "belum_ada_pesanan": {
        ID: "Saat ini kamu belum punya pesanan yang sedang berjalan.",
        EN: "You don't have any order in progress right now.",
    },
    "status_pesanan": {
        ID: ("Status pesanan *{nomor}*: {status} (pembayaran: {bayar})\n"
             "Jumlah item: {jumlah}\nTotal: {total}"),
        EN: ("Order *{nomor}* status: {status} (payment: {bayar})\n"
             "Items: {jumlah}\nTotal: {total}"),
    },
    "tak_ada_tagihan": {
        ID: "Aku tidak menemukan pesanan yang menunggu pembayaran. Mau lihat menu dulu? \U0001F60A",
        EN: "I can't find an order waiting for payment. Would you like to see the menu? \U0001F60A",
    },
    "cek_bayar_gagal": {
        ID: ("Maaf, status pembayaran belum bisa kucek sekarang. "
             "Coba tanya lagi sebentar lagi ya \U0001F64F"),
        EN: ("Sorry, I can't check your payment status right now. "
             "Please ask again in a moment \U0001F64F"),
    },
    "sudah_direfund": {
        ID: ("Pesanan ini sudah dibatalkan dan pembayarannya dikembalikan \u2705\n"
             "Dananya kembali lewat metode pembayaran yang kamu pakai, dan bisa "
             "makan beberapa hari kerja tergantung bank atau e-wallet-nya ya \U0001F64F"),
        EN: ("This order has been cancelled and the payment refunded \u2705\n"
             "The money goes back via the payment method you used, which can take a "
             "few working days depending on your bank or e-wallet \U0001F64F"),
    },
    "menu_gagal": {
        ID: "Maaf, daftar menu sedang tidak bisa diambil. Coba lagi sebentar lagi ya.",
        EN: "Sorry, I can't fetch the menu right now. Please try again shortly.",
    },
    "menu_judul": {
        ID: "Berikut menu {toko}:",
        EN: "Here is the {toko} menu:",
    },
    "menu_habis": {
        ID: "  (sedang tidak tersedia)",
        EN: "  (currently unavailable)",
    },
    "menu_penutup": {
        ID: "\nMau lihat detail salah satu kue? Sebutkan namanya ya \U0001F60A",
        EN: "\nWant details on one of them? Just say its name \U0001F60A",
    },
    # ── Lain-lain ────────────────────────────────────────────────────────────
    "hanya_teks": {
        ID: ("Maaf ya, aku cuma bisa membaca pesan teks 🙏 Voice note, stiker, foto, dan "
             "lokasi belum bisa kuproses. Boleh diketik saja maksudnya? Ketik *menu* "
             "kalau mau lihat daftar kue 😊"),
        EN: ("Sorry, I can only read text messages 🙏 Voice notes, stickers, photos and "
             "locations aren't something I can process. Could you type it instead? "
             "Type *menu* to see our cake list 😊"),
    },
    "nomor_tak_dikenali": {
        ID: ("Maaf, aku belum bisa mengenali nomormu dari sini \U0001F64F Coba kirim "
             "pesan lagi sebentar lagi ya \u2014 kalau masih begini juga, hubungi kami "
             "lewat nomor toko dari kontak yang tersimpan."),
        EN: ("Sorry, I can't recognise your number from here \U0001F64F Please send "
             "another message in a moment \u2014 if it keeps happening, reach us "
             "through our store number from your saved contacts."),
    },
    "di_luar_cakupan": {
        ID: ("Maaf, aku hanya bisa membantu seputar {toko} ya — menu, pemesanan, "
             "pembayaran, pengiriman, dan info toko. Ada yang bisa kubantu soal itu? 😊"),
        EN: ("Sorry, I can only help with {toko} — our menu, orders, payments, "
             "delivery, and store info. Anything I can help with there? 😊"),
    },
}


def teks(kunci: str, lang: str | None = None, **isian) -> str:
    """Balasan tetap `kunci` dalam bahasa `lang`, dengan {isian} sudah diisi.

    Kunci yang tidak dikenal adalah bug pemanggil, bukan keadaan yang perlu
    ditangani pelanggan: dicatat lalu dikembalikan apa adanya supaya kelihatan
    di log dan di uji, bukan diam-diam jadi string kosong.
    """
    varian = _TEMPLAT.get(kunci)
    if varian is None:
        logger.error("Templat '%s' tidak ada", kunci)
        return kunci
    pola = varian.get(normalkan(lang)) or varian[DEFAULT]
    try:
        return pola.format(**isian) if isian else pola
    except KeyError as exc:  # isian kurang -> kelihatan, bukan hilang
        logger.error("Templat '%s' kurang isian %s", kunci, exc)
        return pola


# ── Arahan bahasa untuk model ────────────────────────────────────────────────
# Balasan BEBAS (sapaan, basa-basi, jawaban FAQ) tidak lewat _TEMPLAT, jadi
# bahasanya dulu ditentukan model sendiri tiap giliran -- dan model menebak dari
# teks mentah, bukan dari bahasa sesi. Terukur: "ok" di tengah percakapan
# Indonesia dijawab "Still here 😊 Type *menu* untuk lihat daftarnya ya", 3 dari
# 6 kali, bahkan di temperature 0,3. Sesi sudah tahu jawabannya (deteksi() +
# lengket lewat store.set_lang) -- kalimat ini yang menyampaikannya ke model.
_ARAHAN = {
    ID: ("\n- BAHASA: pelanggan ini memakai Bahasa Indonesia. Balas HANYA dalam "
         "Bahasa Indonesia, termasuk untuk sapaan dan jawaban singkat."),
    EN: ("\n- LANGUAGE: this customer speaks English. Reply ONLY in English, "
         "including greetings and short acknowledgements."),
}


def arahan(lang: str | None) -> str:
    """Satu baris perintah bahasa untuk ditempel ke blok system.

    Bentuknya konstan per bahasa, bukan per giliran: prefix KV-cache Ollama
    tetap utuh selama pelanggan tidak berganti bahasa di tengah percakapan.
    """
    return _ARAHAN[normalkan(lang)]


def semua_kunci() -> list[str]:
    return sorted(_TEMPLAT)
