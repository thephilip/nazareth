"""The memory service: every read and write the MCP server and CLI perform.

Write policy (decided under the ledger lock, then recorded in the event):

- A record can only supersede records that are currently active. Superseding a
  stale record is an error that names the record now in effect, so an agent
  working from old context is corrected instead of silently forking history.
- A decision on a topic that already has an active decision must supersede it
  explicitly. If it doesn't, it is recorded as *pending* for human review.
- Only the human may supersede what the human decided. A lens that tries is
  recorded as *pending* too.
- Pending records have no effect until the human resolves them.
"""

from __future__ import annotations

import fcntl
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from nazareth.ledger.chain import Ledger, LedgerEntry
from nazareth.memory import graph as graphs
from nazareth.memory import vectors
from nazareth.memory.projection import DOMAIN_ACTIONS, Projection
from nazareth.paths import StatePaths

HUMAN = "human"
BRIEF_DECISION_LIMIT = 100
SEARCH_CANDIDATES = 200
VECTOR_CANDIDATES = 50
GRAPH_FACTS = 20
FACTS_SHOWN = 5

_DEFAULT: Any = object()


class NazarethError(Exception):
    """An anticipated failure whose message is written for the caller to act on."""


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _norm_topic(topic: str) -> str:
    return re.sub(r"[\s_]+", "-", topic.strip().lower())


def _clean_ids(ids: list[str] | None) -> list[str]:
    return list(dict.fromkeys(i.strip() for i in ids or [] if i.strip()))


def parse_since(since: str) -> float:
    """'30m', '8h', '7d', or an ISO date/time → epoch seconds."""
    s = since.strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smhdw])", s)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return time.time() - float(m.group(1)) * mult
    try:
        dt = datetime.fromisoformat(since.strip())
    except ValueError:
        raise NazarethError(
            f"Can't parse since={since!r}. Use a duration like '30m', '8h', '7d' or an ISO date."
        ) from None
    return (dt if dt.tzinfo else dt.astimezone()).timestamp()


def _record_out(row: sqlite3.Row, *, full: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"id": row["id"], "kind": row["kind"]}
    if row["topic"]:
        out["topic"] = row["topic"]
    out["text"] = row["text"]
    out["status"] = row["status"]
    if full:
        if row["rationale"]:
            out["rationale"] = row["rationale"]
        out["actor"] = row["actor"]
        out["ts"] = _iso(row["ts"])
        if row["status_reason"]:
            out["status_reason"] = row["status_reason"]
        if row["superseded_by"]:
            out["superseded_by"] = row["superseded_by"]
    return out


def _question_out(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {"id": row["id"], "question": row["question"], "status": row["status"]}
    for key in ("context", "ref", "answer", "answered_by"):
        if row[key]:
            out[key] = row[key]
    out["asked_by"] = row["actor"]
    out["ts"] = _iso(row["ts"])
    if row["answered_ts"]:
        out["answered_ts"] = _iso(row["answered_ts"])
    return out


def _event_out(e: LedgerEntry | sqlite3.Row) -> dict[str, Any]:
    get = (lambda k: getattr(e, k)) if isinstance(e, LedgerEntry) else (lambda k: e[k])
    out = {"id": get("id"), "ts": _iso(get("ts")), "actor": get("actor"),
           "action": get("action"), "summary": get("summary")}
    if get("ref"):
        out["ref"] = get("ref")
    return out


INSTALLER = "https://raw.githubusercontent.com/thephilip/nazareth/main/install.sh"


def install_hint(extras: str) -> str:
    """How to add extras to the CLI's own environment. nazareth isn't on PyPI, and
    a bare `pip install` would land in some other Python, so name both real routes."""
    return (f"Reinstall with the extra: `curl -LsSf {INSTALLER} | sh -s -- --extras {extras}`, "
            f"or from a checkout `uv tool install -e '.[{extras}]' --force`.")


@dataclass(frozen=True)
class EnrichStep:
    """One event sent by `Memory.enrich`, for progress reporting."""

    n: int
    target: int
    kind: str
    id: str
    seconds: float
    facts: int


class Memory:
    """`embedder` defaults to the search extra's model when it's installed; pass
    None for keyword search only, or any `vectors.Embedder`. `graph` opens a
    `graphs.Graph` (called with whether it will write); by default Graphiti is
    used when the graph extra is installed."""

    def __init__(self, paths: StatePaths, embedder: Any = _DEFAULT,
                 graph: Callable[[bool], graphs.Graph] | None | Any = _DEFAULT) -> None:
        if not paths.initialized:
            raise NazarethError(
                f"Nazareth isn't initialized for {paths.project}. "
                "Ask the human to run `nazareth init` there."
            )
        self.paths = paths
        self.ledger = Ledger(paths.ledger)
        self.index = Projection(paths.index, self.ledger)
        self.index.sync()
        self.embedder: vectors.Embedder | None = (
            vectors.default_embedder() if embedder is _DEFAULT else embedder)
        self.vectors = vectors.VectorStore(paths.vectors, on_create=paths.ensure_gitignore)
        # The vector store has its own connection and lock, so semantic work
        # (model load, embedding) can run while other calls use the index.
        self._vector_lock = threading.Lock()
        # Why semantic search is off for this process (the embedder can't load).
        self.vector_error: str | None = None
        # Why the last inline search here fell back to keywords, if it did.
        self.vector_note: str | None = None
        self._open_graph = self._default_graph if graph is _DEFAULT else graph
        self._graph: graphs.Graph | None = None
        self._graph_lock = threading.Lock()
        self.graph_error: str | None = None

    def _default_graph(self, write: bool) -> graphs.Graph:
        if not graphs.graph_extra_installed():
            raise graphs.GraphUnavailable("graph extra not installed")
        return graphs.GraphitiGraph(self.paths.graph, self.embedder,
                                    llm=graphs.llm_settings() if write else None)

    def close(self) -> None:
        self.index.close()
        self.vectors.close()
        with self._graph_lock:
            if self._graph is not None:
                self._graph.close()
                self._graph = None

    def rebuild(self) -> None:
        """Replay the index from the ledger and drop the derived vectors and graph."""
        self.index.rebuild()
        with self._vector_lock:
            self.vectors.reset()
        if self.paths.graph.exists() and self._open_graph is not None:
            with self._enrich_lock(), self._graph_lock:
                try:
                    g = self._graph or self._open_graph(False)
                    getattr(g, "reset", lambda: None)()
                    g.close()
                except graphs.GraphUnavailable:
                    pass
                self._graph = None
                for path in self.paths.state.glob("graph.db*"):
                    path.unlink(missing_ok=True)

    @contextmanager
    def _enrich_lock(self) -> Iterator[None]:
        with open(self.paths.graph_lock, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def enrich(self, limit: int | None = None,
               progress: Callable[[EnrichStep], None] | None = None) -> dict[str, Any]:
        """Send ledger events the graph hasn't seen to Graphiti (`nazareth
        enrich`), oldest first, at most `limit` of them. This is the only call
        that reaches an LLM, and it is never made on the write path.
        `progress` is called after each event sent. The result lists the
        events that produced no facts, which usually means the model failed
        to extract any rather than that there was nothing to learn."""
        if self._open_graph is None:
            raise NazarethError("The knowledge graph is turned off for this process.")
        self.index.sync()
        self.paths.ensure_gitignore()
        with self._enrich_lock(), self._graph_lock:
            try:
                g = self._open_graph(True)
            except graphs.GraphUnavailable as e:
                hint = (install_hint("search,graph") if e.missing_extra else
                        "See the README's knowledge graph section for the LLM settings.")
                raise NazarethError(f"Knowledge graph unavailable: {e}. {hint}") from e
            try:
                mark = g.watermark()
                if mark is not None:
                    seen = self.ledger.entries(after_seq=mark[0] - 1)[:1]
                    if not seen or seen[0].hash != mark[1]:
                        raise NazarethError(
                            "The knowledge graph doesn't match this ledger (it was built from a "
                            "different history). Run `nazareth rebuild`, then `nazareth enrich`.")
                todo = self.ledger.entries(after_seq=mark[0] if mark else -1)
                episodes = [graphs.episode(e, self._kind_of) for e in todo]
                target = sum(ep is not None for ep in episodes)
                if limit is not None:
                    target = min(target, limit)
                sent, done, empty = 0, 0, []
                for e, ep in zip(todo, episodes, strict=True):
                    if ep is not None:
                        if sent >= target:
                            break
                        started = time.monotonic()
                        facts = g.add(ep)
                        sent += 1
                        if not facts:
                            empty.append(ep.target[0])
                        if progress is not None:
                            progress(EnrichStep(sent, target, ep.target[1], ep.target[0],
                                                time.monotonic() - started, facts))
                    g.set_watermark(e.seq, e.hash)
                    done += 1
            finally:
                g.close()
        return {"sent": sent, "remaining": len(todo) - done, "no_facts": empty}

    def _kind_of(self, rid: str) -> str | None:
        row = self.index.record(rid)
        return row["kind"] if row is not None else None

    def embed(self) -> dict[str, Any]:
        """Bring the vector store up to date (`nazareth embed`)."""
        if self.embedder is None:
            raise NazarethError("Semantic search isn't installed. " + install_hint("search"))
        self.index.sync()
        docs = self.index.documents()
        try:
            with self._vector_lock:
                embedded = self.vectors.sync(docs, self.embedder)
                count = self.vectors.count()
        except vectors.EmbedderUnavailable as e:
            raise NazarethError(f"Semantic search unavailable: {e}.") from e
        return {"embedder": self.embedder.name, "count": count, "embedded": embedded}

    # -- internals ------------------------------------------------------------

    def _new_id(self, prefix: str) -> str:
        while True:
            rid = f"{prefix}-{uuid.uuid4().hex[:6]}"
            if self.index.record(rid) is None and self.index.question(rid) is None:
                return rid

    def _require_record(self, rid: str) -> sqlite3.Row:
        row = self.index.record(rid)
        if row is None:
            raise NazarethError(f"Unknown record id {rid!r}. Use `query` to find current IDs.")
        return row

    def _check_supersedable(self, ids: list[str], kind: str) -> list[sqlite3.Row]:
        rows = []
        for rid in ids:
            row = self._require_record(rid)
            if row["kind"] != kind:
                raise NazarethError(f"{rid} is a {row['kind']}; a {kind} can only supersede {kind}s.")
            status = row["status"]
            if status == "superseded":
                current = self.index.current_successor(rid)
                raise NazarethError(
                    f"{rid} was already superseded; {current} is the {kind} in effect now. "
                    f"Read it with query(id={current!r}) and supersede {current} instead."
                )
            if status == "pending":
                raise NazarethError(f"{rid} is pending human review and can't be superseded yet.")
            if status != "active":
                raise NazarethError(f"{rid} is {status} and no longer in effect; nothing to supersede.")
            rows.append(row)
        return rows

    def _related(self, text: str, exclude: set[str], limit: int = 5) -> list[dict[str, Any]]:
        out = []
        for rid, kind in self.index.search(text):
            if kind in ("decision", "constraint") and rid not in exclude:
                row = self.index.record(rid)
                if row is not None and row["status"] == "active":
                    out.append(_record_out(row, full=False))
            if len(out) >= limit:
                break
        return out

    def _write_record(
        self,
        kind: str,
        actor: str,
        text: str,
        rationale: str,
        topic: str | None,
        supersedes: list[str] | None,
        depends_on: list[str] | None,
    ) -> dict[str, Any]:
        if not text.strip():
            raise NazarethError(f"The {kind} text is empty.")
        supersedes, depends_on = _clean_ids(supersedes), _clean_ids(depends_on)
        warnings: list[str] = []
        with self.ledger.transaction():
            self.index.sync()
            superseded_rows = self._check_supersedable(supersedes, kind)
            for rid in depends_on:
                row = self._require_record(rid)
                if row["status"] != "active":
                    current = self.index.current_successor(rid)
                    hint = f"; {current} is in effect now" if current != rid else ""
                    warnings.append(f"depends_on {rid} which is {row['status']}{hint}.")

            reasons = []
            if kind == "decision":
                conflicts = [r["id"] for r in self.index.records(kind="decision", status="active",
                                                                  topic=topic)
                             if r["id"] not in supersedes]
                if conflicts:
                    reasons.append(
                        f"topic {topic!r} already has active decision(s) {', '.join(conflicts)} "
                        "that this doesn't supersede"
                    )
            if actor != HUMAN:
                human_owned = [r["id"] for r in superseded_rows if r["actor"] == HUMAN]
                if human_owned:
                    reasons.append(f"it supersedes human-made {', '.join(human_owned)}")
            status = "pending" if reasons else "active"

            rid = self._new_id("dec" if kind == "decision" else "con")
            record = {"id": rid, "topic": topic, "text": text.strip(), "rationale": rationale.strip(),
                      "status": status, "supersedes": supersedes, "depends_on": depends_on}
            if reasons:
                record["status_reason"] = "; ".join(reasons)
            self.ledger.append(actor=actor, action=kind, summary=text.strip(), ref=rid,
                               payload={"record": record})
        self.index.sync()

        out: dict[str, Any] = {"id": rid, "status": status}
        if status == "active":
            if supersedes:
                out["superseded"] = supersedes
        else:
            out["pending_reason"] = record["status_reason"]
            out["note"] = (
                "Not in effect until the human resolves it. To replace the existing record "
                "yourself, call again with supersedes=[...] listing it; otherwise carry on "
                "without relying on this one."
            )
        related = self._related(f"{topic or ''} {text}", exclude={rid, *supersedes})
        if related:
            out["related_active"] = related
        if warnings:
            out["warnings"] = warnings
        return out

    # -- writes ---------------------------------------------------------------

    def decide(self, topic: str, decision: str, rationale: str, actor: str,
               supersedes: list[str] | None = None,
               depends_on: list[str] | None = None) -> dict[str, Any]:
        topic = _norm_topic(topic)
        if not topic:
            raise NazarethError("A decision needs a topic (e.g. 'auth', 'storage').")
        return self._write_record("decision", actor, decision, rationale, topic,
                                  supersedes, depends_on)

    def constrain(self, constraint: str, reason: str, actor: str, topic: str | None = None,
                  supersedes: list[str] | None = None) -> dict[str, Any]:
        topic = _norm_topic(topic) if topic else None
        return self._write_record("constraint", actor, constraint, reason, topic or None,
                                  supersedes, None)

    def ask(self, question: str, actor: str, context: str | None = None,
            ref: str | None = None) -> dict[str, Any]:
        if not question.strip():
            raise NazarethError("The question is empty.")
        with self.ledger.transaction():
            self.index.sync()
            qid = self._new_id("q")
            payload = {"id": qid, "question": question.strip()}
            if context:
                payload["context"] = context.strip()
            self.ledger.append(actor=actor, action="question", summary=question.strip(),
                               ref=ref, payload=payload)
        self.index.sync()
        return {"id": qid, "status": "open",
                "note": "The human will answer asynchronously; answers show up in `brief`."}

    def answer(self, question_id: str, answer: str, actor: str = HUMAN) -> dict[str, Any]:
        with self.ledger.transaction():
            self.index.sync()
            row = self.index.question(question_id)
            if row is None:
                raise NazarethError(f"Unknown question id {question_id!r}.")
            if row["status"] != "open":
                raise NazarethError(f"{question_id} is already {row['status']}.")
            self.ledger.append(actor=actor, action="answer", summary=answer.strip(),
                               ref=question_id,
                               payload={"question_id": question_id, "answer": answer.strip()})
        self.index.sync()
        return {"id": question_id, "status": "answered"}

    def resolve(self, record_id: str, accept: bool, note: str | None = None,
                actor: str = HUMAN) -> dict[str, Any]:
        """Accept or reject a pending record. Accepting a decision also
        supersedes whatever active decisions it conflicted with on its topic."""
        dropped: list[str] = []
        with self.ledger.transaction():
            self.index.sync()
            row = self._require_record(record_id)
            if row["status"] != "pending":
                raise NazarethError(f"{record_id} is {row['status']}, not pending.")
            payload: dict[str, Any] = {"id": record_id,
                                       "outcome": "accepted" if accept else "rejected"}
            if note:
                payload["note"] = note
            if accept:
                wanted = [l["dst"] for l in self.index.links(src=record_id, link_type="supersedes")]
                if row["kind"] == "decision":
                    wanted += [r["id"] for r in self.index.records(
                        kind="decision", status="active", topic=row["topic"])]
                supersedes = []
                for rid in dict.fromkeys(wanted):
                    target = self.index.record(rid)
                    if target is not None and target["status"] == "active":
                        supersedes.append(rid)
                    else:
                        dropped.append(rid)
                payload["supersedes"] = supersedes
            self.ledger.append(actor=actor, action="resolution",
                               summary=f"{payload['outcome']} {record_id}", ref=record_id,
                               payload=payload)
        self.index.sync()
        out: dict[str, Any] = {"id": record_id, "status": "active" if accept else "rejected"}
        if accept and payload["supersedes"]:
            out["superseded"] = payload["supersedes"]
        if dropped:
            out["skipped"] = [f"{rid} was no longer active" for rid in dropped]
        return out

    def retire(self, record_id: str, reason: str, actor: str = HUMAN) -> dict[str, Any]:
        with self.ledger.transaction():
            self.index.sync()
            row = self._require_record(record_id)
            if row["status"] != "active":
                raise NazarethError(f"{record_id} is {row['status']}, not active.")
            self.ledger.append(actor=actor, action="retirement", summary=f"retired {record_id}",
                               ref=record_id, payload={"id": record_id, "reason": reason})
        self.index.sync()
        return {"id": record_id, "status": "retired"}

    def set_phase(self, actor: str, current: str | None = None,
                  next_phase: str | None = None) -> dict[str, Any]:
        if current is None and next_phase is None:
            raise NazarethError("Give a new current phase, a new next phase, or both.")
        with self.ledger.transaction():
            self.index.sync()
            phase = self.index.phase()
            new = {"current": (current or phase["current"]).strip(),
                   "next": (next_phase or phase["next"]).strip()}
            self.ledger.append(actor=actor, action="phase",
                               summary=f"phase {new['current']} (next: {new['next']})",
                               payload=new)
        self.index.sync()
        return new

    def log(self, action: str, summary: str, actor: str, ref: str | None = None,
            severity: str | None = None) -> dict[str, Any]:
        action = action.strip().lower()
        if not action or not summary.strip():
            raise NazarethError("log needs an action and a summary.")
        if action in DOMAIN_ACTIONS:
            raise NazarethError(
                f"{action!r} is reserved. Use decide/constrain/ask/phase for state changes; "
                "log is for events like 'artifact', 'finding', 'test', 'refactor'."
            )
        payload = {"severity": severity.strip().lower()} if severity else {}
        entry = self.ledger.append(actor=actor, action=action, summary=summary.strip(),
                                   ref=ref, payload=payload)
        self.index.sync()
        return {"id": entry.id, "seq": entry.seq}

    # -- reads ----------------------------------------------------------------

    def phase(self) -> dict[str, str]:
        self.index.sync()
        return self.index.phase()

    def get(self, item_id: str) -> dict[str, Any]:
        self.index.sync()
        item_id = item_id.strip()
        row = self.index.record(item_id)
        if row is not None:
            out = _record_out(row)
            if row["status"] == "superseded":
                out["current"] = self.index.current_successor(item_id)
            for key, rows in (
                ("supersedes", self.index.links(src=item_id, link_type="supersedes")),
                ("depends_on", self.index.links(src=item_id, link_type="depends_on")),
            ):
                if rows:
                    out[key] = [r["dst"] for r in rows]
            dependents = self.index.links(dst=item_id, link_type="depends_on")
            if dependents:
                out["dependents"] = [r["src"] for r in dependents]
            return out
        q = self.index.question(item_id)
        if q is not None:
            return _question_out(q)
        ev = self.index.event(item_id)
        if ev is not None:
            out = _event_out(ev)
            if ev["severity"]:
                out["severity"] = ev["severity"]
            return out
        raise NazarethError(f"Nothing with id {item_id!r}.")

    @property
    def semantic_enabled(self) -> bool:
        return self.embedder is not None and self.vector_error is None

    def search_documents(self) -> list[tuple[str, str, str]]:
        """The corpus to embed. Uses the index connection, so callers sharing
        this Memory across threads must hold their lock."""
        self.index.sync()
        return self.index.documents()

    def semantic(self, text: str,
                 docs: list[tuple[str, str, str]]) -> tuple[list[tuple[str, str]], str | None]:
        """Vector neighbours of `text` as (id, kind), after embedding any of
        `docs` not yet embedded, plus why it fell back to nothing, if it did.

        Touches only the embedder and the vector store, never the index, so it
        is safe to run without the caller's index lock. If the embedder can't
        load, semantic search is off for the life of this process; any other
        failure (e.g. a locked database) only affects this one search."""
        if not self.semantic_enabled or not text.strip():
            return [], self.vector_error
        try:
            with self._vector_lock:
                self.vectors.sync(docs, self.embedder)
                hits = self.vectors.nearest(
                    text, self.embedder, limit=VECTOR_CANDIDATES,
                    min_similarity=getattr(self.embedder, "min_similarity", 0.0))
        except vectors.EmbedderUnavailable as e:
            self.vector_error = str(e) or type(e).__name__
            return [], self.vector_error
        except Exception as e:  # noqa: BLE001 - never fail a search over enrichment
            return [], f"{type(e).__name__}: {e}"
        return [(rid, kind) for rid, kind, _ in hits], None

    def facts(self, text: str) -> list[graphs.Fact]:
        """Knowledge graph facts about `text`, once `nazareth enrich` has built
        the graph. Touches only the graph, never the index, so it is safe to run
        without the caller's index lock. Never fails a search: if the graph
        can't be opened it's off for this process; other errors skip it once."""
        if self._open_graph is None or self.graph_error is not None or not text.strip():
            return []
        if not self.paths.graph.exists():
            return []
        try:
            with self._graph_lock:
                if self._graph is None:
                    self._graph = self._open_graph(False)
                return self._graph.search(text, GRAPH_FACTS)
        except graphs.GraphUnavailable as e:
            self.graph_error = str(e) or type(e).__name__
        except Exception:  # noqa: BLE001 - never fail a search over enrichment
            return []
        return []

    def search(self, text: str, include_inactive: bool = False, limit: int = 10,
               semantic: list[tuple[str, str]] | None = None,
               facts: list[graphs.Fact] | None = None) -> dict[str, Any]:
        """Keyword (FTS5) and, when available, vector and knowledge graph
        search, fused by reciprocal rank. Pass `semantic` (from `semantic()`)
        and `facts` (from `facts()`) to reuse results computed elsewhere, e.g.
        outside a lock; otherwise they're computed here."""
        self.index.sync()
        records, questions, events = [], [], []
        keyword = self.index.search(text, limit=SEARCH_CANDIDATES)
        if semantic is None:
            semantic, self.vector_note = self.semantic(text, self.index.documents()) \
                if self.semantic_enabled else ([], self.vector_error)
        if facts is None:
            facts = self.facts(text)
        from_graph = list(dict.fromkeys(src for f in facts for src in f.sources))
        rankings = [r for r in (keyword, semantic, from_graph) if r]
        ranked = vectors.rrf(*rankings) if len(rankings) > 1 else (rankings or [[]])[0]
        for rid, kind in ranked:
            if kind in ("decision", "constraint") and len(records) < limit:
                row = self.index.record(rid)
                if row is None:
                    continue
                if row["status"] in ("active", "pending"):
                    records.append(_record_out(row))
                elif include_inactive:
                    out = _record_out(row)
                    if row["status"] == "superseded":
                        out["current"] = self.index.current_successor(rid)
                    records.append(out)
            elif kind == "question" and len(questions) < limit:
                q = self.index.question(rid)
                if q is not None:
                    questions.append(_question_out(q))
            elif kind == "event" and len(events) < limit:
                ev = self.index.event(rid)
                if ev is not None:
                    events.append(_event_out(ev))
        out: dict[str, Any] = {"records": records, "questions": questions, "events": events}
        if facts:
            out["facts"] = [{"fact": f.text, "sources": [rid for rid, _ in f.sources]}
                            for f in facts[:FACTS_SHOWN]]
        return out

    def history(self, since: str | None = None, actor: str | None = None,
                action: str | None = None, limit: int = 50) -> dict[str, Any]:
        entries = self.ledger.query(
            actor=actor, action=action, since=parse_since(since) if since else None,
            limit=max(1, min(limit, 500)),
        )
        return {"entries": [_event_out(e) for e in entries], "count": len(entries)}

    def brief(self) -> dict[str, Any]:
        """Everything a lens needs to orient itself, in one call."""
        self.index.sync()
        decisions = self.index.records(kind="decision", status="active")
        out: dict[str, Any] = {
            "phase": self.index.phase(),
            "constraints": [_record_out(r, full=False)
                            for r in self.index.records(kind="constraint", status="active")],
            "decisions": [_record_out(r, full=False) for r in decisions[:BRIEF_DECISION_LIMIT]],
            "active_decision_count": len(decisions),
        }
        if len(decisions) > BRIEF_DECISION_LIMIT:
            out["decisions_truncated"] = (
                f"{len(decisions) - BRIEF_DECISION_LIMIT} more active decisions; use query."
            )
        pending = self.index.records(status="pending")
        if pending:
            out["pending_review"] = [_record_out(r, full=False) | {"reason": r["status_reason"]}
                                     for r in pending]
        open_q = self.index.questions(status="open")
        if open_q:
            out["open_questions"] = [_question_out(q) for q in open_q]
        answered = self.index.questions(status="answered", limit=10, order="answered_ts")
        if answered:
            out["recent_answers"] = [_question_out(q) for q in answered]
        out["recent_activity"] = [_event_out(e) for e in self.ledger.query(limit=10)]
        return out
