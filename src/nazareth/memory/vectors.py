"""LanceDB vector index for semantic search over ledger events and decisions.

Provides a fallback semantic search path when graph traversal doesn't cover
the query. Embeddings are generated via the same the LLM gateway endpoint used by Graphiti.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import lancedb
import numpy as np

from nazareth.memory.graph import _get_gateway_key

TABLE_NAME = "memory"
DIMENSION = 1536


async def _embed_text(texts: list[str]) -> list[list[float]]:
    """Generate embeddings via the the LLM gateway OpenAI-compatible endpoint."""
    from openai import AsyncOpenAI

    api_key = _get_gateway_key()
    base_url = os.environ.get("LLM_GATEWAY_BASE_URL", "https://llm-gateway.example.com/v1")
    model = os.environ.get("LLM_GATEWAY_EMBEDDING_MODEL", "text-embedding-3-small")

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    response = await client.embeddings.create(input=texts, model=model)
    return [item.embedding for item in response.data]


class VectorIndex:
    def __init__(self, db_path: Path) -> None:
        self._db = lancedb.connect(str(db_path))
        self._ensure_table()

    def _ensure_table(self) -> None:
        if TABLE_NAME not in self._db.table_names():
            import pyarrow as pa

            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("category", pa.string()),
                pa.field("actor", pa.string()),
                pa.field("ts", pa.float64()),
                pa.field("vector", pa.list_(pa.float32(), DIMENSION)),
            ])
            self._db.create_table(TABLE_NAME, schema=schema)
        self._table = self._db.open_table(TABLE_NAME)

    async def add(
        self,
        id: str,
        text: str,
        category: str,
        actor: str,
        ts: float | None = None,
    ) -> None:
        embeddings = await _embed_text([text])
        self._table.add([{
            "id": id,
            "text": text,
            "category": category,
            "actor": actor,
            "ts": ts or time.time(),
            "vector": embeddings[0],
        }])

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        embeddings = await _embed_text([query])
        results = (
            self._table.search(embeddings[0])
            .limit(limit)
            .to_list()
        )
        return [
            {
                "id": r["id"],
                "text": r["text"],
                "category": r["category"],
                "actor": r["actor"],
                "ts": r["ts"],
                "score": r.get("_distance", None),
            }
            for r in results
        ]
