"""Tanggal ambil/kirim pesanan: dibaca dari jawaban pelanggan tanpa model.

Backend mewajibkan `fulfillment_date` paling cepat besok (aturan H-1). Toko
menambah batas atas: paling lama 30 hari ke depan. Semua hitungan memakai WIB,
dan nilai yang dikirim ke backend selalu membawa zona waktu, karena backend
menganggap tanggal tanpa zona sebagai UTC.
"""

import re
from datetime import date, datetime, time, timedelta, timezone

WIB = timezone(timedelta(hours=7))
MAKS_HARI = 30
# Jam buka toko. Juga menjaga nilai tetap >= 07.00 WIB: batas backend dihitung
# pada tengah malam UTC (= 07.00 WIB), jadi jam lebih pagi akan ditolaknya.
JAM_BUKA, JAM_TUTUP, JAM_BAWAAN = 7, 17, 10

_BULAN = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "mei": 5, "may": 5, "jun": 6,
          "jul": 7, "agu": 8, "ags": 8, "aug": 8, "sep": 9, "okt": 10, "oct": 10,
          "nov": 11, "des": 12, "dec": 12}
_HARI = {"senin": 0, "monday": 0, "selasa": 1, "tuesday": 1, "rabu": 2,
         "wednesday": 2, "kamis": 3, "thursday": 3, "jumat": 4, "jum'at": 4,
         "friday": 4, "sabtu": 5, "saturday": 5, "minggu": 6, "ahad": 6, "sunday": 6}
_NAMA_HARI = {"id": ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"],
              "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                     "Saturday", "Sunday"]}
_NAMA_BULAN = {"id": ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
                      "Agustus", "September", "Oktober", "November", "Desember"],
               "en": ["January", "February", "March", "April", "May", "June", "July",
                      "August", "September", "October", "November", "December"]}

_JAM_RE = re.compile(r"\b(?:jam|pukul|pkl|at)\s*(\d{1,2})(?:[.:](\d{2}))?\s*(am|pm|pagi|siang|sore)?")
_ANGKA_RE = re.compile(r"\b(\d{1,2})\s*[/-]\s*(\d{1,2})(?:\s*[/-]\s*(\d{2,4}))?\b")
_NAMA_RE = re.compile(r"\b(\d{1,2})\s+([a-z]{3})[a-z]*(?:\s+(\d{4}))?|\b([a-z]{3})[a-z]*\s+(\d{1,2})\b")
_TGL_RE = re.compile(r"\b(?:tanggal|tgl)\s*(\d{1,2})\b")
_N_HARI_RE = re.compile(r"\b(\d{1,2})\s*(?:hari|days?)\b")


def sekarang() -> datetime:
    return datetime.now(WIB)


def _aman(tahun: int, bulan: int, hari: int) -> date | None:
    try:
        return date(tahun, bulan, hari)
    except ValueError:
        return None


def _berikutnya(bulan: int, hari: int, hari_ini: date) -> date | None:
    """Tanggal tanpa tahun: kemunculan berikutnya setelah hari ini."""
    d = _aman(hari_ini.year, bulan, hari)
    if d is None or d <= hari_ini:
        d = _aman(hari_ini.year + 1, bulan, hari) or d
    return d


def baca_tanggal(teks: str, hari_ini: date) -> date | None:
    """Tanggal yang dimaksud pelanggan, atau None bila tidak terbaca.

    Tidak memeriksa rentang; itu tugas `periksa`.
    """
    low = (teks or "").lower()
    if "lusa" in low or "day after tomorrow" in low:
        return hari_ini + timedelta(days=2)
    if "besok" in low or "tomorrow" in low or "bsk" in low.split():
        return hari_ini + timedelta(days=1)
    if "hari ini" in low or "today" in low or "sekarang" in low:
        return hari_ini
    m = _ANGKA_RE.search(low)
    if m:
        hari, bulan = int(m.group(1)), int(m.group(2))
        if m.group(3):
            tahun = int(m.group(3))
            return _aman(tahun + 2000 if tahun < 100 else tahun, bulan, hari)
        return _berikutnya(bulan, hari, hari_ini) if 1 <= bulan <= 12 else None
    for m in _NAMA_RE.finditer(low):
        if m.group(1):
            hari, nama, tahun = int(m.group(1)), m.group(2), m.group(3)
        else:
            hari, nama, tahun = int(m.group(5)), m.group(4), None
        if nama in _BULAN:
            if tahun:
                return _aman(int(tahun), _BULAN[nama], hari)
            return _berikutnya(_BULAN[nama], hari, hari_ini)
    m = _TGL_RE.search(low)
    if m:  # "tanggal 6": bulan ini bila belum lewat, kalau tidak bulan depan
        hari = int(m.group(1))
        d = _aman(hari_ini.year, hari_ini.month, hari)
        if d is None or d <= hari_ini:
            awal_depan = (hari_ini.replace(day=1) + timedelta(days=32)).replace(day=1)
            d = _aman(awal_depan.year, awal_depan.month, hari)
        return d
    m = _N_HARI_RE.search(low)
    if m:
        return hari_ini + timedelta(days=int(m.group(1)))
    if "minggu depan" in low or "pekan depan" in low or "next week" in low:
        return hari_ini + timedelta(days=7)
    for kata in re.findall(r"[a-z']+", low):
        if kata in _HARI:  # nama hari: kemunculan berikutnya, bukan hari ini
            selisih = (_HARI[kata] - hari_ini.weekday()) % 7 or 7
            return hari_ini + timedelta(days=selisih)
    return None


def baca_jam(teks: str) -> int | None:
    """Jam (0-23) bila pelanggan menyebutnya dengan 'jam/pukul/at', selain itu None."""
    m = _JAM_RE.search((teks or "").lower())
    if not m:
        return None
    jam, penanda = int(m.group(1)), m.group(3)
    if penanda in ("pm", "sore", "siang") and jam < 12 and not (penanda == "siang" and jam >= 10):
        jam += 12
    return jam if 0 <= jam <= 23 else None


def periksa(teks: str, kini: datetime | None = None) -> tuple[str | None, str]:
    """(iso, "") bila sah, atau (None, alasan).

    alasan: "tidak_terbaca" | "terlalu_cepat" | "terlalu_jauh" | "jam_tutup".
    """
    kini = kini or sekarang()
    hari_ini = kini.astimezone(WIB).date()
    d = baca_tanggal(teks, hari_ini)
    if d is None:
        return None, "tidak_terbaca"
    if d <= hari_ini:
        return None, "terlalu_cepat"
    if d > hari_ini + timedelta(days=MAKS_HARI):
        return None, "terlalu_jauh"
    jam = baca_jam(teks)
    if jam is None:
        jam = JAM_BAWAAN
    elif not JAM_BUKA <= jam < JAM_TUTUP:
        return None, "jam_tutup"
    return datetime.combine(d, time(jam), tzinfo=WIB).isoformat(), ""


def tampil(iso: str, lang: str | None = None) -> str:
    """'Selasa, 6 Oktober 2026 pukul 10.00' untuk dibaca pelanggan."""
    dt = datetime.fromisoformat(iso)
    en = (lang or "").lower().startswith("en")
    k = "en" if en else "id"
    hari, bulan = _NAMA_HARI[k][dt.weekday()], _NAMA_BULAN[k][dt.month - 1]
    if en:
        return f"{hari}, {dt.day} {bulan} {dt.year} at {dt:%H:%M}"
    return f"{hari}, {dt.day} {bulan} {dt.year} pukul {dt:%H.%M}"


def selisih_hari(iso: str, kini: datetime | None = None) -> int:
    kini = kini or sekarang()
    return (datetime.fromisoformat(iso).date() - kini.astimezone(WIB).date()).days
