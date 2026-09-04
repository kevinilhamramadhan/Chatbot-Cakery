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

from langchain_text_splitters import RecursiveCharacterTextSplitter  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.rag.store import get_collection  # noqa: E402

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger("ingest")

# chunk_size/overlap in config are token-oriented; approximate ~4 chars/token.
CHARS_PER_TOKEN = 4

BOOT_WAIT_INTERVAL_SECONDS = 10

# Sidik jari ditaruh di dalam direktori Chroma, jadi ia ikut volume datanya:
# volume dihapus -> sidik jari hilang -> ingest jalan lagi. Persis yang diinginkan.
MARKER_NAME = ".ingest-fingerprint"


def _file_id(path: Path, idx: int) -> str:
    h = hashlib.sha1(str(path.name).encode()).hexdigest()[:10]
    return f"{h}-{idx}"


def _fingerprint(files: list[Path]) -> str:
    """Identitas hasil ingest: isi FAQ + semua setelan yang mengubah vektornya.

    Ganti model embedding atau ukuran chunk = vektor lama tidak sebanding lagi,
    jadi keduanya ikut dihitung — bukan cuma isi berkasnya.
    """
    h = hashlib.sha256()
    for path in files:
        h.update(path.name.encode())
        h.update(path.read_bytes())
    h.update(
        json.dumps(
            {
                "embedding_model": settings.embedding_model,
                "chunk_size": settings.rag_chunk_size,
                "chunk_overlap": settings.rag_chunk_overlap,
                "collection": settings.chroma_collection,
            },
            sort_keys=True,
        ).encode()
    )
    return h.hexdigest()


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


def ingest(files: list[Path]) -> int:
    collection = get_collection()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.rag_chunk_size * CHARS_PER_TOKEN,
        chunk_overlap=settings.rag_chunk_overlap * CHARS_PER_TOKEN,
    )

    total_chunks = 0
    for path in files:
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            continue
        # Drop any previous chunks for this file (idempotent re-ingest).
        collection.delete(where={"source": path.name})

        chunks = splitter.split_text(text)
        ids = [_file_id(path, i) for i in range(len(chunks))]
        metadatas = [{"source": path.name, "chunk": i} for i in range(len(chunks))]
        collection.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        total_chunks += len(chunks)
        logger.info("Ingested %s -> %d chunk(s)", path.name, len(chunks))

    logger.info(
        "Done. %d file(s), %d chunk(s) in collection '%s' at %s",
        len(files), total_chunks, settings.chroma_collection, settings.chroma_persist_dir,
    )
    return total_chunks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boot",
        action="store_true",
        help="mode container: tunggu Ollama, lewati kalau sudah mutakhir, jangan pernah gagal",
    )
    args = parser.parse_args()

    kb_dir = Path(settings.knowledge_base_dir)
    files = sorted(kb_dir.glob("*.txt")) if kb_dir.exists() else []
    if not files:
        logger.error("Tidak ada berkas FAQ di %s", kb_dir)
        return 0 if args.boot else 1

    if not args.boot:
        ingest(files)
        return 0

    marker = _marker_path()
    fingerprint = _fingerprint(files)
    try:
        if marker.read_text().strip() == fingerprint and get_collection().count() > 0:
            # Dua syarat, bukan satu: sidik jari bisa saja tertinggal padahal
            # koleksinya kosong (mis. volume Chroma diganti isinya).
            logger.info("FAQ sudah mutakhir di Chroma — tidak ada yang perlu dikerjakan.")
            return 0
    except (OSError, Exception) as exc:  # noqa: BLE001 — apa pun berarti "ingest saja"
        logger.info("Belum ada hasil ingest yang cocok (%s); melanjutkan.", exc)

    if not _wait_for_embedding_model():
        return 0  # pesan errornya sudah dicetak; jangan gagalkan stack

    try:
        if ingest(files) > 0:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(fingerprint)
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
