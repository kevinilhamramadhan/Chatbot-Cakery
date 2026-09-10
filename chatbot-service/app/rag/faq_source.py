"""Where the FAQ knowledge base comes from, and how it gets into Chroma.

The backend's `/faq` table is the source of truth: that is the CRUD an admin
uses to edit the bot's answers from Admin Site. The .txt files under
knowledge_base/faq/ are the fallback for when the backend has no FAQ yet or
cannot be reached — without them a fresh install would answer every general
question with "di luar cakupan".

Kept out of knowledge_base/ingest.py so the running service can re-ingest too:
an admin who edits an FAQ should not have to wait for a redeploy.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx
from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.core.config import settings
from app.rag.store import get_collection

logger = logging.getLogger(__name__)

# chunk_size/overlap in config are token-oriented; approximate ~4 chars/token.
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class FaqDoc:
    """One retrievable FAQ topic. `source` keys its chunks in Chroma."""

    source: str
    text: str


def _doc_id(source: str, idx: int) -> str:
    return f"{hashlib.sha1(source.encode()).hexdigest()[:10]}-{idx}"


async def fetch_backend_faq() -> list[FaqDoc]:
    """Active FAQ rows from the backend, newest id last. Empty list on any failure.

    Only `is_active` items: switching an FAQ off in Admin Site has to take the
    answer out of the bot's mouth, not just hide it from a list somewhere.
    """
    url = f"{settings.backend_base_url.rstrip('/')}/faq"
    headers = (
        {"X-Service-Key": settings.backend_service_api_key}
        if settings.backend_service_api_key
        else {}
    )
    try:
        async with httpx.AsyncClient(timeout=settings.backend_request_timeout_seconds,
                                     headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code >= 400:
            logger.warning("Backend /faq -> %s; memakai berkas FAQ lokal", resp.status_code)
            return []
        rows = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Backend /faq tidak terbaca (%s); memakai berkas FAQ lokal", exc)
        return []
    if not isinstance(rows, list):
        return []

    docs: list[FaqDoc] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("is_active", True):
            continue
        q = str(row.get("pertanyaan") or "").strip()
        a = str(row.get("jawaban") or "").strip()
        if not q or not a:
            continue
        docs.append(FaqDoc(source=f"backend-faq-{row.get('id')}", text=f"Q: {q}\nA: {a}"))
    return docs


def local_faq_docs() -> list[FaqDoc]:
    """The .txt fallback shipped inside the image."""
    kb_dir = Path(settings.knowledge_base_dir)
    if not kb_dir.exists():
        return []
    docs = []
    for path in sorted(kb_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8").strip()
        if text:
            docs.append(FaqDoc(source=path.name, text=text))
    return docs


async def current_docs() -> tuple[list[FaqDoc], str]:
    """The FAQ the bot should be answering from, plus where it came from."""
    docs = await fetch_backend_faq()
    if docs:
        return docs, "backend"
    return local_faq_docs(), "berkas lokal"


# Sidik jari hasil ingest terakhir, ditaruh di dalam direktori Chroma supaya ia
# ikut volume datanya: volume dihapus -> sidik jari hilang -> ingest jalan lagi.
# Dibaca DUA proses: container ingest saat boot, dan service yang berjalan saat
# memeriksa perubahan FAQ. Keduanya harus sepakat, kalau tidak service akan
# meng-embed ulang tiap restart (atau lebih buruk: tidak pernah meng-embed
# ulang padahal isi Chroma datang dari sumber yang berbeda).
MARKER_NAME = ".ingest-fingerprint"


def marker_path() -> Path:
    return Path(settings.chroma_persist_dir) / MARKER_NAME


def read_marker() -> str | None:
    try:
        return marker_path().read_text().strip()
    except OSError:
        return None


def write_marker(value: str) -> None:
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


def fingerprint(docs: list[FaqDoc]) -> str:
    """Identity of an ingest result: the FAQ text plus every setting that
    changes the vectors. A different embedding model or chunk size means the old
    vectors are no longer comparable, so those count too."""
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda d: d.source):
        h.update(d.source.encode())
        h.update(d.text.encode())
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


def ingest_documents(docs: list[FaqDoc]) -> int:
    """Replace the collection's contents with `docs`. Returns the chunk count.

    Sources that are no longer present are deleted, not merely left alone: an
    FAQ an admin removed must stop being retrievable, and the old per-file
    delete-then-upsert could never notice a deletion.
    """
    collection = get_collection()
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=settings.rag_chunk_size * CHARS_PER_TOKEN,
        chunk_overlap=settings.rag_chunk_overlap * CHARS_PER_TOKEN,
    )

    wanted = {d.source for d in docs}
    try:
        existing = collection.get(include=["metadatas"])
        stale = {
            (m or {}).get("source")
            for m in existing.get("metadatas", [])
            if (m or {}).get("source") not in wanted
        }
        for source in filter(None, stale):
            collection.delete(where={"source": source})
            logger.info("FAQ '%s' sudah tidak ada — vektornya dihapus", source)
    except Exception as exc:  # noqa: BLE001 - a fresh collection has nothing to clean
        logger.debug("Tidak bisa membaca isi koleksi untuk pembersihan: %s", exc)

    total = 0
    for d in docs:
        collection.delete(where={"source": d.source})  # idempotent re-ingest
        chunks = splitter.split_text(d.text)
        collection.upsert(
            ids=[_doc_id(d.source, i) for i in range(len(chunks))],
            documents=chunks,
            metadatas=[{"source": d.source, "chunk": i} for i in range(len(chunks))],
        )
        total += len(chunks)
        logger.info("Ingested %s -> %d chunk(s)", d.source, len(chunks))
    return total
