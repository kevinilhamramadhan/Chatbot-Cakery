"""ChromaDB persistent store + retrieval with an out-of-scope guard.

The scope guard (PROMPT §7) is the important bit: if the best retrieval
similarity is below RAG_SIMILARITY_THRESHOLD, retrieval is considered
out-of-scope and the caller must refuse to answer from general LLM knowledge.
"""

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.config import settings
from app.rag.embeddings import get_embedding_function

if TYPE_CHECKING:
    import chromadb

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    documents: list[str]
    metadatas: list[dict]
    similarities: list[float]

    @property
    def best_similarity(self) -> float:
        return max(self.similarities) if self.similarities else 0.0

    @property
    def in_scope(self) -> bool:
        return self.best_similarity >= settings.rag_similarity_threshold

    @property
    def relevant_documents(self) -> list[str]:
        """Only the chunks that clear the threshold on their OWN score.

        `retrieve()` always returns top_k chunks, so a question that matches one
        FAQ well drags in the next two regardless of how weakly they match
        (measured: "bisa dikirim ke luar kota?" -> pengiriman 0.404, but also
        pembayaran 0.348 and halal 0.299). Feeding all three dilutes the context
        and the small model then answers from the wrong one — or falls back to
        repeating its previous reply. One good chunk beats three mixed ones.
        """
        threshold = settings.rag_similarity_threshold
        return [
            doc for doc, sim in zip(self.documents, self.similarities) if sim >= threshold
        ]

    def context_text(self) -> str:
        return "\n\n---\n\n".join(self.relevant_documents)


_client = None
# retrieve() runs in a worker thread (see agent.run_agent), so three WhatsApp
# messages arriving together really do enter this module in parallel. Building
# the PersistentClient twice at once killed one of them with "Could not connect
# to tenant default_tenant" and that customer got no reply at all. Reentrant
# because get_collection() holds it across get_client().
_client_lock = threading.RLock()


def get_client():
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import chromadb  # lazy: heavy import

                _client = chromadb.PersistentClient(path=settings.chroma_persist_dir)
    return _client


def get_collection():
    with _client_lock:
        return get_client().get_or_create_collection(
            name=settings.chroma_collection,
            embedding_function=get_embedding_function(),
            metadata={"hnsw:space": "cosine"},
        )


def retrieve(query: str, top_k: int | None = None) -> RetrievalResult:
    top_k = top_k or settings.rag_top_k
    collection = get_collection()
    if collection.count() == 0:
        logger.warning("Chroma collection is empty — run ingest.py first.")
        return RetrievalResult([], [], [])

    # Embed the query ourselves and pass query_embeddings so we don't depend on
    # ChromaDB's internal query-embedding dispatch (which differs across versions).
    query_vec = get_embedding_function().embed_one(query)
    res = collection.query(query_embeddings=[query_vec], n_results=top_k)
    docs = res.get("documents", [[]])[0]
    metas = res.get("metadatas", [[]])[0]
    distances = res.get("distances", [[]])[0]
    # cosine distance -> similarity
    sims = [1.0 - float(d) for d in distances]
    return RetrievalResult(documents=docs, metadatas=metas, similarities=sims)
