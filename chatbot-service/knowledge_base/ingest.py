"""Embed every FAQ file into ChromaDB. Idempotent — safe to re-run.

Each file in knowledge_base/faq/*.txt is one topic/question. We re-ingest a file
by first deleting its previous chunks (keyed by `source`), so edits and deletions
don't leave stale vectors behind.

Dua cara pakai:

    python knowledge_base/ingest.py            # manual: selalu ingest ulang
    python knowledge_base/ingest.py --boot     # mode container (lihat di bawah)

`--boot` dipakai service one-shot `chatbot-ingest` di compose deploy, supaya
server tidak perlu langkah manual apa pun sesudah `docker compose up -d`:

  1. menunggu model embedding siap di Ollama (unduhan pertama ~1,1 GB);
  2. melewati pekerjaan kalau isi Chroma sudah cocok dengan FAQ di image ini
     (sidik jari disimpan di volume), jadi restart stack bukan ingest ulang;
  3. TIDAK PERNAH keluar dengan status gagal. chatbot-service menunggu service
     ini selesai (`service_completed_successfully`) hanya untuk urutan — supaya
     tidak ada dua proses menulis Chroma bersamaan. Kalau kegagalan di sini ikut
     menggagalkan container, chatbot tidak akan pernah hidup gara-gara FAQ; itu
     lebih buruk daripada chatbot hidup tanpa FAQ. Kegagalannya tetap terlihat
     jelas di `docker compose logs chatbot-ingest`.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Allow running as a plain script from chatbot-service/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.rag import faq_source  # noqa: E402
from app.rag.store import get_collection  # noqa: E402

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("ingest")

BOOT_WAIT_INTERVAL_SECONDS = 10

# Sidik jari ditaruh di dalam direktori Chroma, jadi ia ikut volume datanya:
# volume dihapus -> sidik jari hilang -> ingest jalan lagi. Persis yang diinginkan.
MARKER_NAME = ".ingest-fingerprint"


def _marker_path() -> Path:
    return Path(settings.chroma_persist_dir) / MARKER_NAME


def _wait_for_embedding_model(timeout: float | None = None) -> bool:
    """Tunggu sampai Ollama benar-benar PUNYA model embeddingnya.

    Bukan sekadar "Ollama menjawab": container ollama menyalakan servernya dulu,
    baru menarik model di latar belakang. Ingest yang mulai di sela itu gagal
    dengan 404 model not found.
    """
    timeout = settings.ingest_wait_timeout_seconds if timeout is None else timeout
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    wanted = settings.embedding_model
    # Ollama menormalkan "nama" jadi "nama:latest" — samakan sebelum dibandingkan.
    wanted_full = wanted if ":" in wanted else f"{wanted}:latest"
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                names = {m.get("name", "") for m in json.load(resp).get("models", [])}
            if wanted_full in names or wanted in names:
                logger.info("Model embedding '%s' siap di Ollama.", wanted)
                return True
            reason = f"tersedia: {sorted(names) or 'belum ada model'}"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = f"Ollama belum menjawab ({exc})"
        # Satu baris tiap ~1 menit, bukan tiap 10 detik: log `docker compose
        # logs` tidak perlu dibanjiri selama unduhan 1 GB.
        if attempt % 6 == 1:
            logger.info("Menunggu '%s' — %s", wanted, reason)
        time.sleep(BOOT_WAIT_INTERVAL_SECONDS)
    logger.error(
        "Model embedding '%s' tidak siap dalam %.1f menit. FAQ TIDAK di-ingest; "
        "chatbot tetap dijalankan tapi pertanyaan FAQ akan dijawab 'di luar "
        "cakupan'. Perbaiki container ollama lalu ulangi: "
        "docker compose up -d --force-recreate chatbot-ingest",
        wanted,
        timeout / 60,
    )
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boot",
        action="store_true",
        help="mode container: tunggu Ollama, lewati kalau sudah mutakhir, jangan pernah gagal",
    )
    args = parser.parse_args()

    docs, asal = asyncio.run(faq_source.current_docs())
    if not docs:
        logger.error("Tidak ada FAQ sama sekali — /faq backend kosong DAN tidak "
                     "ada berkas di %s", settings.knowledge_base_dir)
        return 0 if args.boot else 1
    logger.info("Sumber FAQ: %s (%d dokumen)", asal, len(docs))

    if not args.boot:
        faq_source.ingest_documents(docs)
        return 0

    marker = _marker_path()
    fp = faq_source.fingerprint(docs)
    try:
        if marker.read_text().strip() == fp and get_collection().count() > 0:
            # Dua syarat, bukan satu: sidik jari bisa saja tertinggal padahal
            # koleksinya kosong (mis. volume Chroma diganti isinya).
            logger.info("FAQ sudah mutakhir di Chroma — tidak ada yang perlu dikerjakan.")
            return 0
    except (OSError, Exception) as exc:  # noqa: BLE001 — apa pun berarti "ingest saja"
        logger.info("Belum ada hasil ingest yang cocok (%s); melanjutkan.", exc)

    if not _wait_for_embedding_model():
        return 0  # pesan errornya sudah dicetak; jangan gagalkan stack

    try:
        if faq_source.ingest_documents(docs) > 0:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(fp)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Ingest FAQ GAGAL (%s). Chatbot tetap dijalankan tanpa basis "
            "pengetahuan FAQ; ulangi dengan: "
            "docker compose up -d --force-recreate chatbot-ingest",
            exc,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
