"""Derived vector store for semantic search (the optional `search` extra).

Vectors are an enrichment of the projection, never a source of truth: they live
in `.nazareth/vectors.db`, are computed lazily at query time from what the FTS
index already holds, and `nazareth rebuild` simply deletes them. Nothing here
runs on the write path.

numpy and model2vec are imported lazily so the core package works without them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import numpy as np

MODEL2VEC_DEFAULT = "minishlab/potion-base-8M"
EMBED_BATCH = 256

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS vectors (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    embedder TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    vec BLOB NOT NULL
);
"""


class EmbedderUnavailable(Exception):
    """The embedder can't produce vectors here (missing extra, model won't load)."""


@runtime_checkable
class Embedder(Protocol):
    """Turns text into vectors. `name` identifies the model; stored vectors made
    by a different name are discarded and recomputed, never mixed."""

    name: str

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> np.ndarray: ...


class Model2VecEmbedder:
    """Static embeddings via model2vec. The model is fetched from Hugging Face
    on first use (~30 MB, cached) and loaded from the local cache afterwards
    without touching the network."""

    # Below this cosine a vector-only hit is noise, not a related record.
    min_similarity = 0.3

    def __init__(self, model_id: str = MODEL2VEC_DEFAULT) -> None:
        self.model_id = model_id
        self.name = f"model2vec:{model_id}"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            return self._load_locked()

    def _load_locked(self):
        if self._model is None:
            try:
                from huggingface_hub import snapshot_download
                from model2vec import StaticModel
            except ImportError as e:
                raise EmbedderUnavailable(f"search extra not installed ({e.name})") from e
            try:
                try:
                    local = snapshot_download(self.model_id, local_files_only=True)
                except Exception:  # noqa: BLE001 - not cached yet: download it once
                    local = snapshot_download(self.model_id)
                self._model = StaticModel.from_pretrained(local)
            except Exception as e:  # offline, bad cache, hub errors
                raise EmbedderUnavailable(
                    f"can't load {self.model_id}: {type(e).__name__}"
                ) from e
        return self._model

    @property
    def dim(self) -> int:
        return int(self._load().dim)

    def embed(self, texts: list[str]) -> np.ndarray:
        import numpy as np

        return np.asarray(self._load().encode(texts), dtype=np.float32)


def search_extra_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("numpy", "model2vec"))


def default_embedder() -> Embedder | None:
    """The configured embedder, or None when the search extra isn't installed."""
    return Model2VecEmbedder() if search_extra_installed() else None


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _normalize(m: np.ndarray) -> np.ndarray:
    import numpy as np

    norms = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(norms == 0, 1, norms)


class VectorStore:
    """SQLite table of (id, kind, embedder, text hash, float32 vector)."""

    def __init__(self, path: Path, on_create: Callable[[], None] | None = None) -> None:
        self.path = path
        self._on_create = on_create
        self._db: sqlite3.Connection | None = None

    @property
    def db(self) -> sqlite3.Connection:
        if self._db is None:
            if self._on_create is not None and not self.path.exists():
                self._on_create()
            self._db = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                       check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
        return self._db

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None

    def reset(self) -> None:
        """Delete the store; it is rebuilt on the next search or `embed`."""
        self.close()
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def embedder_name(self) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = 'embedder'").fetchone()
        return row[0] if row else None

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def sync(self, docs: Iterable[tuple[str, str, str]], embedder: Embedder) -> int:
        """Bring the store in line with `docs` (id, kind, text): embed what's new
        or changed, drop what's gone, and start over if the embedder changed.
        Returns how many documents were embedded."""
        import numpy as np

        docs = list(docs)
        hashes = {rid: text_hash(body) for rid, _, body in docs}
        stored_by = self.embedder_name()
        if stored_by is not None and stored_by != embedder.name:
            stored = {}
        else:
            stored = dict(self.db.execute("SELECT id, text_hash FROM vectors"))
        todo = [(rid, kind, body) for rid, kind, body in docs if stored.get(rid) != hashes[rid]]
        gone = [rid for rid in stored if rid not in hashes]
        if not todo and not gone and stored_by == embedder.name:
            return 0

        # Embedding happens outside any write transaction; a concurrent process
        # doing the same work just writes identical rows.
        rows = []
        for i in range(0, len(todo), EMBED_BATCH):
            batch = todo[i:i + EMBED_BATCH]
            vecs = _normalize(np.asarray(embedder.embed([b for _, _, b in batch]),
                                         dtype=np.float32))
            rows += [(rid, kind, embedder.name, hashes[rid], vec.tobytes())
                     for (rid, kind, _), vec in zip(batch, vecs, strict=True)]

        db = self.db
        db.execute("BEGIN IMMEDIATE")
        try:
            if stored_by != embedder.name:
                db.execute("DELETE FROM vectors")
                db.execute("INSERT INTO meta (key, value) VALUES ('embedder', ?) "
                           "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                           (embedder.name,))
            db.executemany("DELETE FROM vectors WHERE id = ?", [(rid,) for rid in gone])
            db.executemany("INSERT OR REPLACE INTO vectors (id, kind, embedder, text_hash, vec)"
                           " VALUES (?,?,?,?,?)", rows)
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        return len(rows)

    def nearest(self, query: str, embedder: Embedder, limit: int = 50,
                min_similarity: float = 0.0) -> list[tuple[str, str, float]]:
        """Brute-force cosine top-k: (id, kind, similarity), best first."""
        import numpy as np

        rows = self.db.execute("SELECT id, kind, vec FROM vectors WHERE embedder = ?",
                               (embedder.name,)).fetchall()
        if not rows:
            return []
        matrix = np.frombuffer(b"".join(r[2] for r in rows), dtype=np.float32)
        matrix = matrix.reshape(len(rows), -1)
        q = _normalize(np.asarray(embedder.embed([query]), dtype=np.float32))[0]
        if q.shape[0] != matrix.shape[1]:
            return []
        sims = matrix @ q
        top = np.argsort(-sims)[:limit]
        return [(rows[i][0], rows[i][1], float(sims[i])) for i in top
                if sims[i] > min_similarity]


def rrf(*rankings: Sequence[tuple[str, str]], k: int = 60) -> list[tuple[str, str]]:
    """Reciprocal rank fusion of (id, kind) rankings, best first."""
    scores: dict[tuple[str, str], float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=lambda item: -scores[item])
