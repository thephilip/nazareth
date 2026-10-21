"""Derived knowledge graph via Graphiti (the optional `graph` extra).

The graph is an enrichment of the ledger, never a source of truth. Only
`nazareth enrich` writes to it: each ledger event becomes one Graphiti episode,
and Graphiti's LLM extracts entities and facts from it. That is the only place
an LLM is called, so the write path and the lens servers stay LLM-free.
Searching needs just the local embedder and BM25. Every fact points back to the
records it came from, and `nazareth rebuild` deletes the graph.

graphiti-core is imported lazily so the core package works without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import signal
import threading
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from nazareth.ledger.chain import LedgerEntry
from nazareth.memory.projection import RECORD_ACTIONS

GROUP = "nazareth"
WATERMARK_KEY = "nazareth:watermark"
LLM_ENV = ("NAZARETH_LLM_BASE_URL", "NAZARETH_LLM_API_KEY", "NAZARETH_LLM_MODEL")
SMALL_MODEL_ENV = "NAZARETH_LLM_SMALL_MODEL"


class GraphUnavailable(Exception):
    """The graph can't be used here (missing extra or configuration)."""


@dataclass(frozen=True)
class Episode:
    """One ledger event as Graphiti sees it. `target` is the (id, kind) the
    event is about, which is what a fact from this episode points back to."""

    event_id: str
    ts: float
    target: tuple[str, str]
    body: str

    @property
    def name(self) -> str:
        return f"{self.target[1]}:{self.target[0]}:{self.event_id}"


@dataclass(frozen=True)
class Fact:
    text: str
    sources: tuple[tuple[str, str], ...]  # (id, kind), in episode order


def parse_name(name: str) -> tuple[str, str] | None:
    """The (id, kind) an episode name points at, or None if it isn't ours."""
    kind, sep, rest = name.partition(":")
    rid, sep2, _ = rest.partition(":")
    return (rid, kind) if sep and sep2 and rid else None


def episode(e: LedgerEntry, kind_of: Callable[[str], str | None]) -> Episode | None:
    """What Graphiti should learn from a ledger event, or None for events with
    nothing to learn (phase changes). `kind_of` looks up a record's kind."""
    p = e.payload
    if e.action in RECORD_ACTIONS:
        r = p["record"]
        lines = [f"{e.actor} recorded {r['status']} {e.action} {r['id']}"
                 + (f" on topic {r['topic']}" if r.get("topic") else "") + f": {r['text']}"]
        if r.get("rationale"):
            lines.append(f"Rationale: {r['rationale']}")
        if r.get("supersedes"):
            lines.append(f"It supersedes {', '.join(r['supersedes'])}.")
        if r.get("depends_on"):
            lines.append(f"It builds on {', '.join(r['depends_on'])}.")
        return Episode(e.id, e.ts, (r["id"], e.action), "\n".join(lines))
    if e.action == "question":
        body = f"{e.actor} asked the human ({p['id']}): {p['question']}"
        if p.get("context"):
            body += f"\nContext: {p['context']}"
        return Episode(e.id, e.ts, (p["id"], "question"), body)
    if e.action == "answer":
        return Episode(e.id, e.ts, (p["question_id"], "question"),
                       f"{e.actor} answered {p['question_id']}: {p['answer']}")
    if e.action in ("resolution", "retirement"):
        kind = kind_of(p["id"])
        if kind is None:
            return None
        what = p["outcome"] if e.action == "resolution" else "retired"
        body = f"{e.actor} {what} {kind} {p['id']}."
        if p.get("supersedes"):
            body += f" It now supersedes {', '.join(p['supersedes'])}."
        if p.get("note") or p.get("reason"):
            body += f" Note: {p.get('note') or p.get('reason')}"
        return Episode(e.id, e.ts, (p["id"], kind), body)
    if e.action == "phase":
        return None
    body = f"{e.actor} logged {e.action}" + (f" ({e.ref})" if e.ref else "") + f": {e.summary}"
    return Episode(e.id, e.ts, (e.id, "event"), body)


class Graph(Protocol):
    """What Memory needs from a knowledge graph."""

    def watermark(self) -> tuple[int, str] | None:
        """(seq, hash) of the last ledger event handled, or None if empty."""

    def add(self, ep: Episode) -> None: ...

    def set_watermark(self, seq: int, entry_hash: str) -> None: ...

    def search(self, text: str, limit: int) -> list[Fact]: ...

    def close(self) -> None: ...


def graph_extra_installed() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in ("graphiti_core", "redislite"))


def llm_settings() -> dict[str, str]:
    """The extraction LLM, from the environment. Any OpenAI-compatible endpoint."""
    missing = [k for k in LLM_ENV if not os.environ.get(k)]
    if missing:
        raise GraphUnavailable(
            f"set {', '.join(missing)} to an OpenAI-compatible endpoint, key and model")
    out = {k: os.environ[k] for k in LLM_ENV}
    out[SMALL_MODEL_ENV] = os.environ.get(SMALL_MODEL_ENV) or out["NAZARETH_LLM_MODEL"]
    return out


class GraphitiGraph:
    """Graphiti over embedded FalkorDB Lite at `path`.

    Graphiti is async; this runs it on a private event loop thread so the sync
    Memory (and the MCP server's worker threads) can call it. Every process that
    opens the same file shares one embedded server. Writes are saved to disk
    explicitly, since FalkorDB Lite doesn't reliably save on exit."""

    def __init__(self, path: Path, embedder: Any, llm: dict[str, str] | None = None) -> None:
        if not graph_extra_installed():
            raise GraphUnavailable("graph extra not installed")
        if embedder is None:
            raise GraphUnavailable("the graph needs the search extra's embedder")
        os.environ["GRAPHITI_TELEMETRY_ENABLED"] = "false"
        self.path = path
        self._client_name = f"nazareth-{os.getpid()}"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True,
                                        name="nazareth-graph")
        self._thread.start()
        try:
            self._graphiti, self._db = self._run(self._open(embedder, llm))
        except BaseException:
            self._stop()
            raise

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _open(self, embedder: Any, llm: dict[str, str] | None):
        from graphiti_core import Graphiti
        from graphiti_core.cross_encoder.client import CrossEncoderClient
        from graphiti_core.driver.falkordb_driver import FalkorDriver
        from graphiti_core.embedder.client import EmbedderClient
        from graphiti_core.llm_client.config import LLMConfig
        from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
        from redislite.async_falkordb_client import AsyncFalkorDB

        class LocalEmbedder(EmbedderClient):
            """Graphiti's embedder interface over nazareth's own (model2vec)."""

            async def create(self, input_data):
                text = input_data if isinstance(input_data, str) else " ".join(map(str, input_data))
                return embedder.embed([text])[0].tolist()

            async def create_batch(self, input_data_list):
                return [v.tolist() for v in embedder.embed(list(input_data_list))]

        class KeepOrder(CrossEncoderClient):
            """No reranking: search uses reciprocal rank fusion, not a model."""

            async def rank(self, query, passages):
                return [(p, 1.0 - i / max(len(passages), 1)) for i, p in enumerate(passages)]

        # Searching never calls the LLM, so readers get a client that can't reach one.
        config = (LLMConfig(api_key=llm["NAZARETH_LLM_API_KEY"],
                            model=llm["NAZARETH_LLM_MODEL"],
                            small_model=llm[SMALL_MODEL_ENV],
                            base_url=llm["NAZARETH_LLM_BASE_URL"])
                  if llm else LLMConfig(api_key="unused", model="unused",
                                        base_url="http://127.0.0.1:9/v1"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = AsyncFalkorDB(dbfilename=str(self.path), client_name=self._client_name)
        graphiti = Graphiti(graph_driver=FalkorDriver(falkor_db=db, database=GROUP),
                            llm_client=OpenAIGenericClient(config=config),
                            embedder=LocalEmbedder(), cross_encoder=KeepOrder())
        await graphiti.build_indices_and_constraints()
        return graphiti, db

    def watermark(self) -> tuple[int, str] | None:
        raw = self._run(self._db.client.get(WATERMARK_KEY))
        if not raw:
            return None
        seq, _, entry_hash = (raw.decode() if isinstance(raw, bytes) else raw).partition(":")
        return int(seq), entry_hash

    def add(self, ep: Episode) -> None:
        from graphiti_core.nodes import EpisodeType

        self._run(self._graphiti.add_episode(
            name=ep.name, episode_body=ep.body, source=EpisodeType.text,
            source_description="nazareth ledger event", group_id=GROUP,
            reference_time=datetime.fromtimestamp(ep.ts, UTC)))

    def set_watermark(self, seq: int, entry_hash: str) -> None:
        async def save():
            await self._db.client.set(WATERMARK_KEY, f"{seq}:{entry_hash}")
            await self._db.client.save()

        self._run(save())

    def search(self, text: str, limit: int) -> list[Fact]:
        from graphiti_core.nodes import EpisodicNode

        async def go():
            edges = await self._graphiti.search(text, group_ids=[GROUP], num_results=limit)
            edges = [e for e in edges if e.invalid_at is None and e.expired_at is None]
            uuids = list(dict.fromkeys(u for e in edges for u in e.episodes))
            nodes = await EpisodicNode.get_by_uuids(self._graphiti.driver, uuids) if uuids else []
            names = {n.uuid: n.name for n in nodes}
            out = []
            for e in edges:
                sources = [parse_name(names.get(u, "")) for u in e.episodes]
                out.append(Fact(e.fact, tuple(dict.fromkeys(s for s in sources if s))))
            return out

        return self._run(go())

    def reset(self) -> None:
        """Empty the graph, including for other processes sharing the server."""
        async def wipe():
            await self._db.client.flushall()
            await self._db.client.save()

        self._run(wipe())

    def _stop(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def close(self) -> None:
        async def last() -> bool:
            # FalkorDB Lite's own connections are unnamed; ours carry the pid.
            names = {c.get("name") for c in await self._db.client.client_list()}
            return not {n for n in names if n and n != self._client_name}

        # FalkorDB Lite's cleanup never awaits its async shutdown, which would
        # leave the server running, and an awaited SHUTDOWN stalls for seconds.
        # So the last process to close stops the server with SIGTERM (redis
        # saves and exits); other processes still using it keep it alive.
        try:
            server = self._db.client.pid if self._run(last()) else None
        except Exception:  # noqa: BLE001 - already gone: nothing to stop
            server = None
        try:
            self._run(self._graphiti.close())
        finally:
            self._stop()
            if server:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(server, signal.SIGTERM)
