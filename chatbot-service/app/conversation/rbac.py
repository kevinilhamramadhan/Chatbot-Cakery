"""Peran orang yang sedang chat, dan apa yang boleh dilakukannya.

Basis datanya sudah punya hierarki: tabel `roles` menyimpan `level` — 1 Owner,
2 Admin, 3 Staff — dan tiap user menunjuk ke satu role. Modul ini membawa
hierarki itu ke sisi chatbot supaya keputusan "boleh atau tidak" dibuat di satu
tempat, bukan disebar sebagai daftar nomor di beberapa berkas.

Aturannya satu kalimat: **level yang lebih kecil boleh melakukan semua yang
boleh dilakukan level di atasnya.** Owner otomatis mendapat apa pun yang
diberikan ke Admin, tanpa perlu didaftarkan dua kali — itu yang dulu tidak
berlaku, waktu "siapa Owner" dan "siapa yang menerima takeover" hidup sebagai
dua daftar terpisah yang bisa saling bertentangan.

Sumber datanya backend, bukan .env: menambah orang cukup lewat Admin Site.
Endpointnya `GET /admin/takeover-handlers` — nama lamanya dipertahankan supaya
backend tidak perlu memindahkan apa pun. Selama balasannya masih berisi nomor
saja tanpa peran, peran disusun dari dua sumber lama sebagai penambal (lihat
`_direktori_tambalan`); begitu balasannya membawa peran, tambalan itu berhenti
terpakai dengan sendirinya.
"""

import logging
import time
from dataclasses import dataclass

from app.backend_client import api as backend
from app.core.config import settings
from app.core.security import wa_digits

logger = logging.getLogger(__name__)

# Sama persis dengan kolom roles.level di backend.
OWNER = 1
ADMIN = 2
STAFF = 3
PELANGGAN = 99

_NAMA_LEVEL = {OWNER: "Owner", ADMIN: "Admin", STAFF: "Staff"}
_LEVEL_NAMA = {v.casefold(): k for k, v in _NAMA_LEVEL.items()}

# Direktori di-cache: dipanggil di tiap giliran yang menyentuh izin, sementara
# isinya hanya berubah kalau ada yang menyunting user di Admin Site.
_TTL_DETIK = 300.0
_cache: list["Orang"] | None = None
_cache_saat: float = 0.0


@dataclass(frozen=True)
class Orang:
    """Satu orang internal: nomor WhatsApp-nya, perannya, dan level hierarkinya."""

    nomor: str
    peran: str
    level: int
    menerima_takeover: bool


def nomor_wa(nomor: str) -> str:
    """Bentuk internasional yang bisa dialamatkan WhatsApp.

    Kolom `nomor_wa_admin` menerima apa pun yang diketik admin, dan isinya
    pernah "08111111111" — format lokal yang tidak bisa dijangkau WhatsApp,
    sehingga tiap pemberitahuan takeover diam-diam hilang.
    """
    digit = wa_digits(nomor)
    if digit.startswith("0"):
        digit = "62" + digit[1:]
    return digit


def _level_dari_peran(peran: str, level) -> int:
    """Level dari backend kalau ada, kalau tidak diterjemahkan dari nama peran."""
    try:
        angka = int(level)
        if angka > 0:
            return angka
    except (TypeError, ValueError):
        pass
    return _LEVEL_NAMA.get(str(peran or "").strip().casefold(), STAFF)


async def _direktori_backend() -> list[Orang] | None:
    """Direktori dengan peran, kalau backend sudah mengirimkannya."""
    baris = await backend.get_role_directory()
    if baris is None:
        return None
    orang: list[Orang] = []
    for b in baris:
        nomor = nomor_wa(str(b.get("nomor_wa") or b.get("nomor_wa_admin") or ""))
        if len(nomor) < 10:
            continue
        peran = str(b.get("role") or b.get("nama_role") or "").strip()
        level = _level_dari_peran(peran, b.get("level"))
        orang.append(Orang(
            nomor=nomor,
            peran=peran or _NAMA_LEVEL.get(level, "Staff"),
            level=level,
            menerima_takeover=bool(b.get("handles_takeover")),
        ))
    return orang


async def _direktori_tambalan() -> list[Orang]:
    """Susun direktori dari dua sumber lama selama endpoint resminya belum ada.

    `/admin/takeover-handlers` hanya mengembalikan nomor, tanpa peran — jadi
    level penerima takeover ditebak ADMIN, dan nomor yang juga terdaftar sebagai
    Owner di .env dinaikkan ke OWNER. Menebak seperti ini persis alasan endpoint
    direktori diminta: perannya seharusnya datang dari basis data, bukan dari
    tebakan chatbot.
    """
    penerima = [nomor_wa(n) for n in await backend.get_takeover_admin_numbers()]
    pemilik = [nomor_wa(n) for n in settings.owner_wa_list]
    orang: dict[str, Orang] = {}
    for nomor in penerima:
        if len(nomor) >= 10:
            orang[nomor] = Orang(nomor, "Admin", ADMIN, True)
    for nomor in pemilik:
        if len(nomor) < 10:
            continue
        lama = orang.get(nomor)
        orang[nomor] = Orang(
            nomor, "Owner", OWNER, lama.menerima_takeover if lama else False
        )
    return list(orang.values())


async def direktori(paksa_segar: bool = False) -> list[Orang]:
    """Semua orang internal yang dikenal, dari cache kalau masih segar."""
    global _cache, _cache_saat
    if not paksa_segar and _cache is not None and time.monotonic() - _cache_saat < _TTL_DETIK:
        return _cache
    try:
        orang = await _direktori_backend()
        if orang is None:
            orang = await _direktori_tambalan()
    except Exception as exc:  # noqa: BLE001 - izin tidak boleh menjatuhkan giliran
        logger.warning("direktori peran tidak terbaca: %s", exc)
        # Cache lama lebih baik daripada tiba-tiba menganggap semua orang
        # pelanggan biasa: itu akan mematikan tool Owner di tengah percakapan.
        return _cache if _cache is not None else []
    _cache, _cache_saat = orang, time.monotonic()
    return orang


def bersihkan_cache() -> None:
    """Buang cache direktori (dipakai uji, dan saat peran baru saja diubah)."""
    global _cache, _cache_saat
    _cache, _cache_saat = None, 0.0


async def siapa(nomor: str) -> Orang | None:
    """Orang internal pemilik nomor ini, atau None kalau dia pelanggan biasa."""
    target = nomor_wa(nomor)
    for orang in await direktori():
        if orang.nomor == target:
            return orang
    return None


async def level(nomor: str) -> int:
    orang = await siapa(nomor)
    return orang.level if orang else PELANGGAN


async def boleh(nomor: str, minimal: int) -> bool:
    """True kalau pemilik nomor ini berperan `minimal` atau lebih tinggi.

    Lebih tinggi berarti angkanya lebih kecil — Owner (1) lolos pemeriksaan
    Admin (2), jadi Owner tidak perlu didaftarkan ulang untuk tiap kemampuan.
    """
    return await level(nomor) <= minimal


async def nomor_penerima_takeover() -> list[str]:
    """Nomor yang diberi tahu saat percakapan dieskalasi ke manusia.

    Disaring flag `handles_takeover`, bukan peran: Owner yang bertugas menerima
    eskalasi persis seperti Admin.
    """
    urut: list[str] = []
    for orang in await direktori():
        if orang.menerima_takeover and orang.nomor not in urut:
            urut.append(orang.nomor)
    return urut
