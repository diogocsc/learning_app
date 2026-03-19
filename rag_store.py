# rag_store.py
import faiss
import numpy as np
from pathlib import Path
from sentence_transformers import SentenceTransformer
import pickle

EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")

# Path to store vector DB per user/subject
def _db_paths(user_id: int, subject_id: int):
    base = Path("data/rag") / f"user_{user_id}" / f"subject_{subject_id}"
    base.mkdir(parents=True, exist_ok=True)
    return base / "index.faiss", base / "meta.pkl"


def embed_text(texts: list[str]) -> np.ndarray:
    return np.array(EMBED_MODEL.encode(texts, convert_to_numpy=True))


def load_or_create_index(user_id: int, subject_id: int, dim: int = 384):
    index_path, meta_path = _db_paths(user_id, subject_id)

    if index_path.exists() and meta_path.exists():
        index = faiss.read_index(str(index_path))
        with open(meta_path, "rb") as f:
            meta = pickle.load(f)
        return index, meta

    index = faiss.IndexFlatL2(dim)
    meta = []
    return index, meta


def save_index(user_id: int, subject_id: int, index, meta):
    index_path, meta_path = _db_paths(user_id, subject_id)
    faiss.write_index(index, str(index_path))
    with open(meta_path, "wb") as f:
        pickle.dump(meta, f)


def add_documents(user_id: int, subject_id: int, docs: list[str]):
    index, meta = load_or_create_index(user_id, subject_id)

    # Embed+add in batches to avoid huge memory spikes for large PDFs.
    batch_size = 64
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        if not batch:
            continue
        emb = embed_text(batch)
        index.add(emb)
        meta.extend(batch)

    save_index(user_id, subject_id, index, meta)


def clear_index(user_id: int, subject_id: int) -> None:
    """
    Best-effort removal of the subject's persisted FAISS index + metadata.
    Used for track generation, where we need deterministic chunk selection.
    """
    index_path, meta_path = _db_paths(user_id, subject_id)
    if index_path.exists():
        try:
            index_path.unlink()
        except OSError:
            pass
    if meta_path.exists():
        try:
            meta_path.unlink()
        except OSError:
            pass


def retrieve(user_id: int, subject_id: int, query: str, k: int = 5):
    index, meta = load_or_create_index(user_id, subject_id)
    if len(meta) == 0:
        return []

    q = embed_text([query])
    distances, indices = index.search(q, k)
    return [meta[i] for i in indices[0] if i < len(meta)]