"""Shared fixtures. No test may load a real embedding model or touch the network."""

from __future__ import annotations

import hashlib
import re

import numpy as np
import pytest

from nazareth.memory import vectors

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

    def embed(self, texts: list[str]) -> np.ndarray:
        raise vectors.EmbedderUnavailable("can't load model: ConnectionError")


@pytest.fixture(autouse=True)
def _no_real_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to keyword-only search; tests that want vectors inject FakeEmbedder."""
    monkeypatch.setattr(vectors, "default_embedder", lambda: None)
