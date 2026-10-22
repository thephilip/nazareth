"""Shared fixtures. No test may load a real embedding model or touch the network."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import threading

import pytest

try:  # the search extra; tests that need it are marked `needs_search`
    import numpy as np
except ImportError:
    np = None

from nazareth.memory import vectors

needs_search = pytest.mark.skipif(
    np is None or importlib.util.find_spec("model2vec") is None,
    reason="needs the search extra")

# Words the fake embedder treats as one concept, so semantic matches are
# deterministic: "identity" lands next to "actor" though FTS sees no overlap.
SYNONYMS = {
    "identity": "actor", "who": "actor", "persona": "actor",
    "database": "storage", "persistence": "storage", "sqlite": "storage",
}


class FakeEmbedder:
    """Hashed bag of words with a synonym table. Counts what it embeds."""

    dim = 256
    min_similarity = 0.2

    def __init__(self, name: str = "fake:bow-v1") -> None:
        self.name = name
        self.embedded: list[str] = []

    def _bucket(self, word: str) -> int:
        word = SYNONYMS.get(word, word)
        return int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dim

    def embed(self, texts: list[str]) -> np.ndarray:
        self.embedded += texts
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for word in re.findall(r"\w+", text.lower()):
                out[i, self._bucket(word)] += 1.0
        return out


class BrokenEmbedder:
    """Stands in for model2vec when the model can't be downloaded."""

    name = "broken"
    dim = 8

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        raise vectors.EmbedderUnavailable("can't load model: ConnectionError")


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to keyword-only search; tests that want vectors inject FakeEmbedder."""
    monkeypatch.setattr(vectors, "default_embedder", lambda: None)


class FlakyEmbedder(FakeEmbedder):
    """Fails its first `failures` calls with a transient error, then works."""

    def __init__(self, failures: int = 1) -> None:
        super().__init__()
        self.failures = failures

    def embed(self, texts: list[str]) -> np.ndarray:
        if self.failures:
            self.failures -= 1
            raise sqlite3.OperationalError("database is locked")
        return super().embed(texts)


class GatedEmbedder(FakeEmbedder):
    """Blocks its first call until released, like a model still downloading."""

    def __init__(self) -> None:
        super().__init__()
        self.started, self.release = threading.Event(), threading.Event()

    def embed(self, texts: list[str]) -> np.ndarray:
        self.started.set()
        assert self.release.wait(10), "test never released the embedder"
        return super().embed(texts)


@pytest.fixture(autouse=True)
def _no_real_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never open Graphiti by default; tests that want a graph inject FakeGraph."""
    from nazareth.memory import graph
    from nazareth.memory.service import Memory

    def unavailable(self, write: bool):
        raise graph.GraphUnavailable("graph disabled in tests")

    monkeypatch.setattr(Memory, "_default_graph", unavailable)
