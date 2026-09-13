"""Single place that collects all LangChain tools exposed to the LLM."""

import logging

from app.tools.add_to_cart import add_to_cart
from app.tools.cancel_order import cancel_order
from app.tools.compare_products import compare_products
from app.tools.escalate import escalate_to_admin
from app.tools.get_menu import get_menu
from app.tools.get_product_detail import get_product_detail
from app.tools.keluhan import sampaikan_maaf
from app.tools.order_status import get_order_status
from app.tools.payment_info import kirim_ulang_pembayaran
from app.tools.payment_status import check_payment_status
from app.tools.reports import business_analytics, financial_report
from app.tools.view_cart import lihat_keranjang

logger = logging.getLogger(__name__)

# Tool yang boleh dilihat siapa pun yang menyapa nomor toko.
TOOLS_UMUM = [
    get_menu,
    get_product_detail,
    compare_products,
    add_to_cart,
    lihat_keranjang,
    get_order_status,
    check_payment_status,
    kirim_ulang_pembayaran,
    cancel_order,
    escalate_to_admin,
    sampaikan_maaf,
]

# Angka bisnis. Hanya Owner yang boleh melihat bahwa ini ada.
TOOLS_OWNER = [
    financial_report,
    business_analytics,
]

ALL_TOOLS = TOOLS_UMUM + TOOLS_OWNER

TOOLS_BY_NAME = {t.name: t for t in ALL_TOOLS}
NAMA_TOOL_OWNER = {t.name for t in TOOLS_OWNER}


async def tools_untuk(wa_number: str) -> list:
    """Tool yang dimuat untuk penelepon ini.

    Pelanggan biasa tidak cuma ditolak waktu memanggil — definisi tool-nya tidak
    pernah sampai ke model. Bedanya nyata: kalau definisinya ikut dimuat, model
    1,7 B sesekali menyebut namanya di jawaban ("via tool financial_report"),
    dan pelanggan jadi tahu ada laporan keuangan yang bisa diminta. Penjaga di
    dalam tool-nya tetap ada sebagai lapis kedua.

    Gagal membaca direktori peran = dianggap pelanggan biasa: kalau ragu, lebih
    baik Owner kehilangan dua tool sebentar daripada angka toko bocor.
    """
    from app.conversation import rbac

    try:
        if await rbac.boleh(wa_number, rbac.OWNER):
            return ALL_TOOLS
    except Exception:  # noqa: BLE001 - izin tidak boleh menjatuhkan giliran
        logger.warning("peran tidak terbaca, tool Owner tidak dimuat", exc_info=True)
    return TOOLS_UMUM
