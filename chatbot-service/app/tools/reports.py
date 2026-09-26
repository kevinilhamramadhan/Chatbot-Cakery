"""Tools: financial_report / business_analytics — Owner-only, REAL data.

Both consume one backend endpoint: GET /reports/financial-summary (X-Service-Key).
Until the backend ships it, get_report_summary returns None and the tools say
so honestly — no dummy numbers.
Access is gated by role: the sender must be Owner (rbac level 1). The role
comes from the backend user directory, so promoting someone to Owner in Admin
Site is enough — no .env edit, no redeploy.
"""

import re
from datetime import datetime, timedelta, timezone

from langchain_core.tools import tool

from app.backend_client import api as backend
from app.conversation import rbac
from app.conversation.context import get_turn_context
from app.tools.formatting import rupiah

_DENIED = (
    "Maaf, laporan ini hanya untuk Owner Toti Cakery dan nomormu belum terdaftar "
    "sebagai Owner."
)
_UNAVAILABLE = (
    "Laporan belum bisa diambil — endpoint laporan di backend belum tersedia "
    "atau sedang gangguan. Coba lagi nanti ya."
)


async def _is_owner(wa: str) -> bool:
    return await rbac.boleh(wa, rbac.OWNER)


# Toko di Batam: bulan dan hari laporan mengikuti WIB, bukan jam UTC container
# (tanggal 1 pukul 00.00-07.00 WIB dulu masih terhitung bulan sebelumnya).
# WIB tidak punya DST, jadi offset tetap cukup dan tidak butuh tzdata.
_WIB = timezone(timedelta(hours=7))


def _month_range(now: datetime | None = None) -> tuple[str, str]:
    now = (now or datetime.now(timezone.utc)).astimezone(_WIB)
    return now.replace(day=1).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")


_N_HARI_RE = re.compile(r"\b(\d{1,3})\s*(hari|days?)\b")


def periode_dari_teks(text: str, now: datetime | None = None) -> tuple[str, str]:
    """Rentang tanggal (WIB) yang diminta Owner, dibaca dari kalimatnya.

    Tool-nya sengaja tidak diberi parameter periode: model 1,7B dilatih dengan
    tool laporan tanpa argumen, dan mengubah skemanya berarti mengubah prefix
    prompt dan data latih. Kalimat Owner sendiri sudah memuat periodenya —
    dulu diabaikan, jadi "laporan seminggu ini" dijawab laporan sebulan.
    Tanpa petunjuk periode, bulan berjalan tetap jadi bawaan. Docstring kedua
    tool di bawah sengaja tetap "bulan berjalan": teks itu bagian dari definisi
    tool yang dikirim ke model, jadi mengubahnya menggeser prefix prompt Owner.
    """
    hari_ini = (now or datetime.now(timezone.utc)).astimezone(_WIB).date()
    t = (text or "").lower()

    def rentang(awal, akhir) -> tuple[str, str]:
        return awal.isoformat(), akhir.isoformat()

    m = _N_HARI_RE.search(t)
    if m and int(m.group(1)) > 0:
        return rentang(hari_ini - timedelta(days=int(m.group(1)) - 1), hari_ini)
    # "seminggu ini" = tujuh hari terakhir, jadi dicek sebelum "minggu ini".
    if any(k in t for k in ("seminggu", "sepekan", "past week", "last 7 days")):
        return rentang(hari_ini - timedelta(days=6), hari_ini)
    if any(k in t for k in ("minggu lalu", "pekan lalu", "last week")):
        senin_ini = hari_ini - timedelta(days=hari_ini.weekday())
        return rentang(senin_ini - timedelta(days=7), senin_ini - timedelta(days=1))
    if any(k in t for k in ("minggu ini", "pekan ini", "this week")):
        return rentang(hari_ini - timedelta(days=hari_ini.weekday()), hari_ini)
    if any(k in t for k in ("kemarin", "yesterday")):
        kemarin = hari_ini - timedelta(days=1)
        return rentang(kemarin, kemarin)
    if any(k in t for k in ("hari ini", "today")):
        return rentang(hari_ini, hari_ini)
    if any(k in t for k in ("bulan lalu", "last month")):
        akhir = hari_ini.replace(day=1) - timedelta(days=1)
        return rentang(akhir.replace(day=1), akhir)
    if any(k in t for k in ("tahun ini", "this year")):
        return rentang(hari_ini.replace(month=1, day=1), hari_ini)
    return rentang(hari_ini.replace(day=1), hari_ini)


async def _summary() -> tuple[dict | None, str, str]:
    start, end = periode_dari_teks(get_turn_context().user_text)
    return await backend.get_report_summary(start, end), start, end


@tool
async def financial_report() -> str:
    """Laporan keuangan bulan berjalan (khusus Owner): omzet, pengeluaran, laba.
    Gunakan hanya jika pelanggan adalah Owner dan meminta laporan keuangan.
    """
    if not await _is_owner(get_turn_context().wa_number):
        return _DENIED
    data, start, end = await _summary()
    if data is None:
        return _UNAVAILABLE
    revenue = float(data.get("revenue") or 0)
    expenses = float(data.get("expenses") or 0)
    return (
        f"📊 *Laporan Keuangan* ({start} s/d {end})\n"
        f"Omzet (pembayaran masuk): {rupiah(revenue)}\n"
        f"Pengeluaran: {rupiah(expenses)}\n"
        f"Laba kotor: {rupiah(revenue - expenses)}\n"
        f"Jumlah pesanan: {data.get('order_count', '-')}"
    )


@tool
async def business_analytics() -> str:
    """Analitik bisnis bulan berjalan (khusus Owner): produk terlaris, rata-rata
    nilai pesanan. Gunakan hanya jika pelanggan adalah Owner.
    """
    if not await _is_owner(get_turn_context().wa_number):
        return _DENIED
    data, start, end = await _summary()
    if data is None:
        return _UNAVAILABLE
    # Backend tidak mengurutkan top_products; "terlaris" berarti jumlah terjual.
    top = sorted(data.get("top_products") or [], key=lambda p: -(p.get("qty") or 0))
    lines = [f"📈 *Analitik Bisnis* ({start} s/d {end})"]
    if top:
        lines.append("Produk terlaris:")
        for i, p in enumerate(top[:5], 1):
            lines.append(f"{i}) {p.get('nama_produk')} — {p.get('qty')} pcs"
                         f" ({rupiah(p.get('revenue', 0))})")
    else:
        lines.append("Belum ada penjualan di periode ini.")
    lines.append(f"Jumlah pesanan: {data.get('order_count', '-')}")
    lines.append(f"Rata-rata nilai pesanan: {rupiah(data.get('avg_order_value') or 0)}")
    return "\n".join(lines)
