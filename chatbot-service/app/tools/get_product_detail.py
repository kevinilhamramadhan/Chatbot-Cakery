"""Tool: get_product_detail — REAL. Returns text and queues a product photo."""

import logging

import httpx
from langchain_core.tools import tool

from app.conversation.context import OutboundMedia, get_turn_context
from app.core.config import settings
from app.tools.formatting import options_line, product_label, resolve_product, rupiah

logger = logging.getLogger(__name__)


async def _image_exists(url: str) -> bool:
    """Don't hand wwebjs-api a URL that 404s.

    products.image_url points at the backend's own /static, but the backend runs
    on Vercel serverless where the filesystem is ephemeral — uploaded photos are
    gone after the deployment that wrote them (verified: /static/products/20.png
    -> 404). Sending a dead URL makes the gateway throw and the customer gets a
    silent failure right after a normal-looking reply, so the text answer goes
    out on its own instead.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as c:
            r = await c.head(url)
            if r.status_code == 405:  # some hosts reject HEAD; fall back
                r = await c.get(url, headers={"Range": "bytes=0-0"})
        ok = r.status_code < 400
    except httpx.HTTPError as exc:
        logger.info("Product image unreachable (%s): %s", url, exc)
        return False
    if not ok:
        logger.warning("Product image %s -> %s, not sending it", url, r.status_code)
    return ok


@tool
async def get_product_detail(product: str) -> str:
    """Ambil detail satu produk (deskripsi, harga) dan KIRIM fotonya ke pelanggan.

    `product` bisa berupa nama kue atau id produk. Gunakan saat pelanggan
    menanyakan detail/penjelasan satu produk tertentu.
    """
    p, options = await resolve_product(product)
    if p is None:
        if options:
            return (
                f"Untuk '{product}' ada beberapa pilihan: {options_line(options)}. "
                "Yang mana yang mau kamu lihat? 😊"
            )
        return f"Maaf, aku tidak menemukan produk '{product}'. Coba cek menu dulu ya."

    name = product_label(p)
    desc = p.get("deskripsi") or "Belum ada deskripsi untuk produk ini."
    harga = rupiah(p.get("harga_jual"))

    # Queue the image to be sent via wwebjs-api (PROMPT §10.2).
    image_url = p.get("image_url")
    caption = f"{name} — {harga}"
    if image_url:
        if image_url.startswith("/"):
            # DB stores a relative path (/static/products/12.jpg) so every
            # consumer prefixes its own reachable backend base URL.
            image_url = settings.backend_base_url.rstrip("/") + image_url
        if await _image_exists(image_url):
            get_turn_context().media.append(
                OutboundMedia(image_url=image_url, caption=caption)
            )

    return (
        f"*{name}*\n{desc}\nHarga: {harga}\n\n"
        "Mau pesan ini? Bilang aja jumlahnya ya 😊"
    )
