"""Jawaban FAQ dikirim apa adanya, bukan ditulis ulang model.

Model 1,7 B sering salah menyalin FAQ yang sudah benar terambil: "toko buka
hari minggu?" dijawab "Hari ini kami buka", padahal konteksnya berbunyi "Hari
Minggu kami libur" (terukur 6 Okt 2026). Jadi begitu jelas FAQ mana yang
dimaksud, yang dikirim adalah teks jawaban FAQ itu sendiri.

"Jelas" diputuskan dengan dua aturan. Ambangnya dipilih dari uji di server
produksi (6 Okt 2026): 116 pertanyaan FAQ bergaya chat, Indonesia dan Inggris,
yang dijawab model dengan teks.

1. Skor kemiripan pertanyaan >= 0,60 DAN selisih ke FAQ berikutnya >= 0,05:
   FAQ-nya tidak diragukan, apa pun yang ditulis model. 50 terjawab, 0 salah.
2. Skornya di bawah itu, tapi balasan yang ditulis model paling mirip (>= 0,75)
   dengan jawaban salah satu dari dua FAQ teratas: model sudah memilih FAQ itu,
   tinggal teksnya yang diluruskan. 44 terjawab benar; 1 salah, dan di situ
   balasan model sendiri memang sudah mengutip FAQ yang keliru.

Gabungan: 94 dari 116 (81%) menerima teks FAQ yang benar. Sisanya dibiarkan
dijawab model seperti sebelumnya: memaksakan FAQ teratas saat skornya rendah
berarti menjawab salah dengan yakin (FAQ teratas keliru ±1 dari 10).

Yang dicoba dan ditolak karena terukur lebih buruk: variasi pertanyaan per FAQ
(FAQ teratas benar 39 -> 36 dari 48 pada kalimat baru), model 1,7 B sebagai
pemilih FAQ (37 dari 128), dan reranker jina-v2-multilingual (36 dari 48,
2-4 detik per pertanyaan).

Versi Inggris jawaban ada di knowledge_base/faq_en.json, ditulis tangan dan
dikunci ke sidik jawaban Indonesianya: begitu admin menyunting jawabannya di
Admin Site, terjemahan lama otomatis tidak dipakai (sesi Inggris kembali
dijawab model) sampai berkasnya diperbarui. Diterjemahkan model saat ingest
sudah dicoba dan ditolak — "luar kota" jadi "outside the country".
"""

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

from app.conversation import bahasa
from app.core.config import settings
from app.rag.embeddings import get_embedding_function

logger = logging.getLogger(__name__)

# Skor kemiripan pertanyaan (dengan instruksi, lihat embeddings.py).
SKOR_YAKIN = 0.60
JARAK_YAKIN = 0.05
# Kemiripan balasan model dengan jawaban FAQ supaya dianggap "model memilih ini".
MIRIP_BALASAN = 0.75


def _sidik(jawaban: str) -> str:
    return hashlib.sha256(jawaban.encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def _paket_en() -> dict:
    path = Path(settings.knowledge_base_dir).parent / "faq_en.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("faq_en.json tidak terbaca (%s) — FAQ bahasa Inggris dijawab model", exc)
        return {}


def jawaban_untuk(faq: dict, lang: str) -> str | None:
    """Teks baku FAQ ini dalam bahasa sesi; None kalau versi bahasanya tidak ada."""
    if bahasa.normalkan(lang) != bahasa.EN:
        return faq["jawaban"]
    entri = _paket_en().get(faq["pertanyaan"])
    if entri and entri.get("sidik_jawaban") == _sidik(faq["jawaban"]):
        return entri["jawaban_en"]
    return None


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def pilih(kandidat: list[dict], balasan_model: str, lang: str) -> tuple[str, str] | None:
    """(jawaban baku, alasan) atau None = biarkan balasan model.

    Fungsi biasa (bukan async): dipanggil lewat asyncio.to_thread karena
    meng-embed balasan model adalah I/O yang memblokir.
    """
    if not kandidat:
        return None
    atas = kandidat[0]
    jarak = atas["skor"] - kandidat[1]["skor"] if len(kandidat) > 1 else 1.0
    if atas["skor"] >= SKOR_YAKIN and jarak >= JARAK_YAKIN:
        teks = jawaban_untuk(atas, lang)
        return (teks, "skor") if teks else None

    if not (balasan_model or "").strip():
        return None
    ef = get_embedding_function()
    v_balasan = ef.embed_one(balasan_model)
    terbaik, mirip = None, 0.0
    for faq in kandidat[:2]:
        teks = jawaban_untuk(faq, lang)
        if not teks:
            continue
        s = _cos(v_balasan, ef.embed_one(teks))
        if s > mirip:
            terbaik, mirip = teks, s
    if terbaik and mirip >= MIRIP_BALASAN:
        return terbaik, f"balasan {mirip:.2f}"
    return None
