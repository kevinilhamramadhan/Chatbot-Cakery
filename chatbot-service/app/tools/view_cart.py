"""Tool: check_cart — reads back the draft order.

"berapa totalnya sekarang?" used to be answered with the whole menu, because no
tool could see the cart. It is the most natural question a customer asks right
before agreeing to a total, and the cart can change several times before that.
"""

from langchain_core.tools import tool

from app.conversation import bahasa, store
from app.conversation.context import get_turn_context
from app.tools.add_to_cart import cart_summary


@tool
async def check_cart() -> str:
    """Tampilkan isi keranjang/draft pesanan pelanggan beserta totalnya.

    Gunakan saat pelanggan menanyakan pesanannya sejauh ini, totalnya, atau
    ingin memastikan isi keranjang sebelum konfirmasi.
    """
    wa = get_turn_context().wa_number
    lang = await store.get_lang(wa)
    cart = await store.get_cart(wa)
    if not cart:
        return bahasa.teks("keranjang_kosong_tool", lang)
    # Balasan di bawah mengajak "ketik sudah sesuai". Tanpa pindah ke langkah
    # konfirmasi, jawaban itu jatuh ke model lagi — terukur 6 Okt 2026: sesudah
    # tagihan gagal, "sudah sesuai" dijawab get_order_status.
    from app.conversation.states import State

    get_turn_context().next_state = State.AWAITING_CART_CONFIRMATION
    return cart_summary(cart, lang) + bahasa.teks("keranjang_ajak_konfirmasi", lang)
