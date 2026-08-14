"""Real product reads against the backend.

Backend field names are Indonesian: nama_produk, harga_jual, deskripsi, kategori
(see {BACKEND_BASE_URL}/openapi.json -> ProductOut).
"""

import logging

from app.backend_client.base import backend_client

logger = logging.getLogger(__name__)


async def list_products(only_active: bool = True, kategori: str | None = None) -> list[dict]:
    params: dict = {"only_active": str(only_active).lower()}
    if kategori:
        params["kategori"] = kategori
    resp = await backend_client.get("/products/", params=params)
    if resp is None:
        return []
    return resp.json()


async def get_product(product_id: int) -> dict | None:
    resp = await backend_client.get(f"/products/{product_id}")
    if resp is None:
        return None
    return resp.json()
