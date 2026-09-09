"""Tool: lihat_keranjang — reads back the draft order.

"berapa totalnya sekarang?" used to be answered with the whole menu, because no
tool could see the cart. It is the most natural question a customer asks right
before agreeing to a total, and the cart can change several times before that.
"""

from langchain_core.tools import tool

from app.conversation import store
from app.conversation.context import get_turn_context
from app.tools.add_to_cart import cart_summary


@tool
async def lihat_keranjang() -> str:
    """Tampilkan isi keranjang/draft pesanan pelanggan beserta totalnya.

    Gunakan saat pelanggan menanyakan pesanannya sejauh ini, totalnya, atau
    ingin memastikan isi keranjang sebelum konfirmasi.
    """
    cart = await store.get_cart(get_turn_context().wa_number)
    if not cart:
        return ("Keranjangmu masih kosong. Ketik *menu* untuk lihat daftar kue, "
                "atau sebutkan kue dan jumlahnya ya 😊")
    return cart_summary(cart) + "\n\nKetik *sudah sesuai* kalau sudah pas ya 😊"
