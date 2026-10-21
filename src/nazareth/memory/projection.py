"""SQLite projection of the ledger.

Everything here is derived: the ledger is the source of truth, and this index
can be deleted and replayed at any time (`nazareth rebuild`). Status decisions
(active vs. pending) are made at write time and recorded in the event, so a
replay reproduces exactly what happened even if the write policy later changes.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from nazareth.ledger.chain import Ledger, LedgerEntry

# Ledger actions that change projected state. Anything else is a plain event.
RECORD_ACTIONS = {"decision", "constraint"}
DOMAIN_ACTIONS = RECORD_ACTIONS | {"resolution", "retirement", "phase", "question", "answer"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS records (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    topic TEXT,
    text TEXT NOT NULL,
    rationale TEXT,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    status_reason TEXT,
    superseded_by TEXT,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    type TEXT NOT NULL,
    PRIMARY KEY (src, dst, type)
);
CREATE TABLE IF NOT EXISTS questions (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    context TEXT,
    ref TEXT,
    actor TEXT NOT NULL,
    status TEXT NOT NULL,
    answer TEXT,
    answered_by TEXT,
    ts REAL NOT NULL,
    answered_ts REAL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    id TEXT NOT NULL,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    summary TEXT NOT NULL,
    ref TEXT,
    severity TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
    id UNINDEXED, kind UNINDEXED, body, tokenize = 'porter unicode61'
);
"""

_TABLES = ("meta", "records", "links", "questions", "events", "search")

_STOPWORDS = {
    "a", "an", "and", "are", "about", "any", "do", "does", "for", "how", "in", "is", "it",
    "of", "on", "or", "our", "the", "to", "we", "what", "which", "why", "with",
}


def fts_query(text: str) -> str | None:
    """Turn free text into a forgiving FTS5 query: OR of prefix-matched terms."""
    terms = [t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS]
    if not terms:
        return None
    return " OR ".join(f'"{t}"*' for t in dict.fromkeys(terms))


class Projection:
    def __init__(self, path: Path, ledger: Ledger) -> None:
        self._ledger = ledger
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None,
                                  check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- sync -----------------------------------------------------------------

    def _meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def sync(self) -> None:
        """Apply ledger entries this index hasn't seen yet. Safe to call from
        several processes at once: the write transaction serializes them."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            last_seq = int(self._meta("last_seq", "-1"))
            if last_seq >= 0 and not self._still_matches(last_seq):
                self._clear()
                last_seq = -1
            for entry in self._ledger.entries(after_seq=last_seq):
                self._apply(entry)
                last_seq = entry.seq
                self._set_meta("last_seq", str(entry.seq))
                self._set_meta("last_hash", entry.hash)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def rebuild(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        self._clear()
        self.db.execute("COMMIT")
        self.sync()

    def _still_matches(self, last_seq: int) -> bool:
        """False if the ledger no longer contains what this index was built from."""
        tail = self._ledger.entries(after_seq=last_seq - 1)
        return bool(tail) and tail[0].hash == self._meta("last_hash")

    def _clear(self) -> None:
        for table in _TABLES:
            self.db.execute(f"DELETE FROM {table}")

    # -- apply ----------------------------------------------------------------

    def _apply(self, e: LedgerEntry) -> None:
        p = e.payload
        if e.action in RECORD_ACTIONS:
            r = p["record"]
            self.db.execute(
                "INSERT INTO records (id, kind, topic, text, rationale, actor, status,"
                " status_reason, seq, ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["id"], e.action, r.get("topic"), r["text"], r.get("rationale"), e.actor,
                 r["status"], r.get("status_reason"), e.seq, e.ts, e.ts),
            )
            for link_type in ("supersedes", "depends_on"):
                for dst in r.get(link_type, []):
                    self._link(r["id"], dst, link_type)
            if r["status"] == "active":
                self._supersede(r.get("supersedes", []), by=r["id"], ts=e.ts)
            body = " ".join(filter(None, [r.get("topic"), r["text"], r.get("rationale")]))
            self._index(r["id"], e.action, body)
        elif e.action == "resolution":
            if p["outcome"] == "accepted":
                self._set_status(p["id"], "active", p.get("note"), e.ts)
                for dst in p.get("supersedes", []):
                    self._link(p["id"], dst, "supersedes")
                self._supersede(p.get("supersedes", []), by=p["id"], ts=e.ts)
            else:
                self._set_status(p["id"], "rejected", p.get("note"), e.ts)
        elif e.action == "retirement":
            self._set_status(p["id"], "retired", p.get("reason"), e.ts)
        elif e.action == "phase":
            self._set_meta("phase_current", p["current"])
            self._set_meta("phase_next", p["next"])
        elif e.action == "question":
            self.db.execute(
                "INSERT INTO questions (id, question, context, ref, actor, status, ts)"
                " VALUES (?,?,?,?,?,'open',?)",
                (p["id"], p["question"], p.get("context"), e.ref, e.actor, e.ts),
            )
            self._index(p["id"], "question", " ".join(filter(None, [p["question"], p.get("context")])))
        elif e.action == "answer":
            self.db.execute(
                "UPDATE questions SET status = 'answered', answer = ?, answered_by = ?,"
                " answered_ts = ? WHERE id = ?",
                (p["answer"], e.actor, e.ts, p["question_id"]),
            )
        else:
            self.db.execute(
                "INSERT INTO events (seq, id, ts, actor, action, summary, ref, severity)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (e.seq, e.id, e.ts, e.actor, e.action, e.summary, e.ref, p.get("severity")),
            )
            self._index(e.id, "event", " ".join(filter(None, [e.action, e.summary, e.ref])))

    def _link(self, src: str, dst: str, link_type: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO links (src, dst, type) VALUES (?, ?, ?)", (src, dst, link_type)
        )

    def _supersede(self, ids: list[str], by: str, ts: float) -> None:
        for rid in ids:
            self.db.execute(
                "UPDATE records SET status = 'superseded', superseded_by = ?, updated_ts = ?"
                " WHERE id = ?",
                (by, ts, rid),
            )

    def _set_status(self, rid: str, status: str, reason: str | None, ts: float) -> None:
        self.db.execute(
            "UPDATE records SET status = ?, status_reason = ?, updated_ts = ? WHERE id = ?",
            (status, reason, ts, rid),
        )

    def _index(self, rid: str, kind: str, body: str) -> None:
        self.db.execute("INSERT INTO search (id, kind, body) VALUES (?, ?, ?)", (rid, kind, body))

    # -- reads ----------------------------------------------------------------

    def record(self, rid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM records WHERE id = ?", (rid,)).fetchone()

    def records(self, *, kind: str | None = None, status: str | None = None,
                topic: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for col, val in (("kind", kind), ("status", status), ("topic", topic)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.db.execute(
            f"SELECT * FROM records {where} ORDER BY topic, seq", params
        ).fetchall()

    def links(self, *, src: str | None = None, dst: str | None = None,
              link_type: str | None = None) -> list[sqlite3.Row]:
        clauses, params = [], []
        for col, val in (("src", src), ("dst", dst), ("type", link_type)):
            if val is not None:
                clauses.append(f"{col} = ?")
                params.append(val)
        return self.db.execute(
            f"SELECT * FROM links WHERE {' AND '.join(clauses)}", params
        ).fetchall()

    def current_successor(self, rid: str) -> str:
        """Follow superseded_by to the record now in effect."""
        seen = {rid}
        row = self.record(rid)
        while row is not None and row["superseded_by"] and row["superseded_by"] not in seen:
            rid = row["superseded_by"]
            seen.add(rid)
            row = self.record(rid)
        return rid

    def question(self, qid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM questions WHERE id = ?", (qid,)).fetchone()

    def questions(self, *, status: str | None = None, limit: int | None = None,
                  order: str = "ts") -> list[sqlite3.Row]:
        where = "WHERE status = ?" if status else ""
        params: list[Any] = [status] if status else []
        lim = f"LIMIT {int(limit)}" if limit else ""
        return self.db.execute(
            f"SELECT * FROM questions {where} ORDER BY {order} DESC {lim}", params
        ).fetchall()

    def event(self, eid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()

    def search(self, text: str, limit: int = 50) -> list[tuple[str, str]]:
        q = fts_query(text)
        if q is None:
            return []
        rows = self.db.execute(
            "SELECT id, kind FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?", (q, limit)
        ).fetchall()
        return [(r["id"], r["kind"]) for r in rows]

    def phase(self) -> dict[str, str]:
        return {
            "current": self._meta("phase_current", "brainstorm"),
            "next": self._meta("phase_next", "scaffold"),
        }
