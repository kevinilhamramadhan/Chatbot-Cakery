"""Tool: set_language — pelanggan meminta bot berganti bahasa.

Deteksi bahasa per pesan tetap di orchestrator (bahasa.deteksi); tool ini hanya
untuk permintaan EKSPLISIT ("pakai bahasa Inggris aja"), yang kalimatnya sendiri
sering seluruhnya berbahasa Indonesia sehingga deteksi otomatis mengunci ke arah
yang salah.
"""

from typing import Literal

from langchain_core.tools import tool

from app.conversation import bahasa, store
from app.conversation.context import get_turn_context


@tool
async def set_language(language: Literal["id", "en"]) -> str:
    """Ganti bahasa balasan atas permintaan pelanggan ("pakai bahasa Inggris
    aja"). `language`: "id" atau "en"."""
    wa = get_turn_context().wa_number
    lang = bahasa.normalkan(language)
    await store.set_lang(wa, lang)
    return bahasa.teks("bahasa_diganti", lang)
