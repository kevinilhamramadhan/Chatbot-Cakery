"""Batas laju backend (HTTP 429 dari rate limiter).

Backend membatasi jumlah permintaan per IP per endpoint. Chatbot memanggil
backend dari satu container, jadi semua percakapan berbagi satu kuota. Kalau
kuota satu endpoint habis, backend menjawab 429 beserta `Retry-After` (detik
sampai jendelanya dibuka lagi).

Aturan di sini:

- Sesudah 429, endpoint itu tidak dipanggil lagi sampai `Retry-After` lewat.
  Permintaan berikutnya langsung gagal di sisi chatbot (`BackendSibuk`) tanpa
  menyentuh jaringan, karena backend pasti menolaknya juga.
- Tidak ada pengulangan otomatis. Pembuatan pesanan dan tagihan tidak boleh
  diulang sendiri (bisa menjadi pesanan ganda); pembacaan berkala diulang
  dengan sendirinya oleh putaran pekerja latar berikutnya.

429 yang bukan dari rate limiter (misalnya kode verifikasi yang dikunci
sesudah tiga kali salah nomor) adalah jawaban bisnis untuk satu pelanggan dan
tidak boleh menahan endpoint itu untuk pelanggan lain. Keduanya dibedakan dari
header `X-RateLimit-Scope` yang hanya ada pada 429 rate limiter; untuk backend
lama tanpa header itu, dari badannya: rate limiter (SlowAPI) menulis
`{"error": "Rate limit exceeded: ..."}`, jawaban bisnis memakai `{"detail": ...}`.
"""

import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

_BAWAAN_DETIK = 60.0     # jendela rate limiter backend kalau Retry-After tidak ada
_MAKS_DETIK = 300.0      # jangan menahan endpoint lebih lama dari ini

_ditahan_sampai: dict[str, float] = {}


class BackendSibuk(httpx.HTTPStatusError):
    """Kuota rate limit backend untuk endpoint ini sedang habis."""

    def __init__(self, request: httpx.Request, detik: float) -> None:
        self.detik = max(1, round(detik))
        response = httpx.Response(429, request=request,
                                  headers={"Retry-After": str(self.detik)},
                                  json={"error": "Rate limit exceeded (ditahan chatbot)"})
        super().__init__(f"backend rate limit, coba lagi {self.detik} dtk",
                         request=request, response=response)


def _kunci(request: httpx.Request) -> str:
    """Metode + jalur dengan bagian angka disamakan, mendekati satu rute backend."""
    jalur = re.sub(r"/\d+(?=/|$)", "/{n}", request.url.path)
    return f"{request.method} {jalur}"


def dari_limiter(response: httpx.Response) -> bool:
    """True kalau 429 ini berasal dari rate limiter, bukan jawaban bisnis.

    Penanda utamanya header `X-RateLimit-Scope` (`ip` atau `service`), yang
    sejak backend c52820a dipasang di setiap 429 dari rate limiter dan tidak
    pernah di 429 bisnis (HTTPException). Badan balasan hanya cadangan untuk
    backend yang belum mengirim header itu; formatnya bukan kontrak.
    """
    if response.status_code != 429:
        return False
    if response.headers.get("X-RateLimit-Scope", "").strip():
        return True
    try:
        data = response.json()
    except ValueError:
        return True          # tanpa badan JSON: anggap rate limiter
    if not isinstance(data, dict):
        return True
    return "detail" not in data


def detik_tunggu(response: httpx.Response) -> float:
    try:
        detik = float(response.headers.get("Retry-After", ""))
    except ValueError:
        detik = _BAWAAN_DETIK
    return min(max(detik, 1.0), _MAKS_DETIK)


def karena_limit(exc: BaseException) -> bool:
    """True kalau galat ini adalah penolakan rate limiter backend."""
    if isinstance(exc, BackendSibuk):
        return True
    res = getattr(exc, "response", None)
    return isinstance(exc, httpx.HTTPStatusError) and res is not None and dari_limiter(res)


def reset() -> None:
    _ditahan_sampai.clear()


async def _sebelum(request: httpx.Request) -> None:
    kunci = _kunci(request)
    sisa = _ditahan_sampai.get(kunci, 0.0) - time.monotonic()
    if sisa > 0:
        raise BackendSibuk(request, sisa)
    _ditahan_sampai.pop(kunci, None)


async def _sesudah(response: httpx.Response) -> None:
    if response.status_code != 429:
        return
    await response.aread()
    if not dari_limiter(response):
        return
    detik = detik_tunggu(response)
    kunci = _kunci(response.request)
    _ditahan_sampai[kunci] = time.monotonic() + detik
    logger.warning("Backend rate limit pada %s; ditahan %.0f dtk", kunci, detik)


def kait() -> dict:
    """`event_hooks` untuk setiap httpx.AsyncClient yang memanggil backend."""
    return {"request": [_sebelum], "response": [_sesudah]}
