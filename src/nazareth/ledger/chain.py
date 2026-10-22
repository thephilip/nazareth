"""Append-only hash-chained event ledger.

Ported from shem-spolia's event log. Each entry's hash covers the previous
hash plus the canonical form of the event, creating a tamper-evident chain.

The ledger file is the single source of truth for a project, and several
processes (one MCP server per lens, plus the CLI) may append to it at once.
Appends therefore take an exclusive lock on a sidecar lock file and re-read
the file tail before chaining, so every writer extends the true head.
Readers need no lock: they only consume newline-terminated lines.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GENESIS_HASH = hashlib.sha256(b"nazareth-genesis").hexdigest()


def _event_id() -> str:
    return f"evt_{uuid.uuid4().hex[:16]}"


def _canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(prev_hash: str, event_canonical: str) -> str:
    return hashlib.sha256(f"{prev_hash}{event_canonical}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    id: str
    seq: int
    ts: float
    actor: str
    action: str
    summary: str
    ref: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    hash: str = ""
    prev_hash: str = ""

    def canonical_fields(self) -> dict:
        """Fields included in the hash — seq is deliberately excluded."""
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "summary": self.summary,
            "ref": self.ref,
            "payload": self.payload,
        }


class Ledger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path = path.with_name(path.name + ".lock")
        self._entries: list[LedgerEntry] = []
        self._offset = 0
        self._head_hash = GENESIS_HASH
        self._locked = False
        self.refresh()

    @property
    def path(self) -> Path:
        return self._path

    def refresh(self) -> None:
        """Pick up entries other processes appended since the last read."""
        if not self._path.exists():
            return
        if self._path.stat().st_size < self._offset:
            # File was replaced or truncated underneath us; start over.
            self._entries, self._offset, self._head_hash = [], 0, GENESIS_HASH
        with open(self._path, "rb") as f:
            f.seek(self._offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # ignore a trailing partial line mid-write
        for line in data[:end].splitlines():
            if line.strip():
                entry = LedgerEntry(**json.loads(line))
                self._entries.append(entry)
                self._head_hash = entry.hash
        self._offset += end

    @contextmanager
    def transaction(self) -> Iterator[Ledger]:
        """Hold the write lock across reads and appends, so a caller can check
        state and append based on it without another writer slipping in between."""
        if self._locked:
            yield self
            return
        with open(self._lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._locked = True
            try:
                self.refresh()
                yield self
            finally:
                self._locked = False
                fcntl.flock(lock, fcntl.LOCK_UN)

    def append(
        self,
        actor: str,
        action: str,
        summary: str,
        ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        with self.transaction():
            entry = LedgerEntry(
                id=_event_id(),
                seq=len(self._entries),
                ts=time.time(),
                actor=actor,
                action=action,
                summary=summary,
                ref=ref,
                payload=payload or {},
                prev_hash=self._head_hash,
            )
            canonical = _canonical(entry.canonical_fields())
            entry = LedgerEntry(**{**asdict(entry), "hash": _hash(self._head_hash, canonical)})
            line = (json.dumps(asdict(entry), separators=(",", ":")) + "\n").encode()
            with open(self._path, "ab") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self._entries.append(entry)
            self._head_hash = entry.hash
            self._offset += len(line)
            return entry

    def verify(self) -> tuple[bool, str]:
        """Walk the chain on disk and verify every link. Returns (valid, message)."""
        self.refresh()
        expected = GENESIS_HASH
        for i, entry in enumerate(self._entries):
            if entry.seq != i:
                return False, f"Chain broken at seq {i} (entry {entry.id}): seq is {entry.seq}"
            if entry.prev_hash != expected:
                return False, f"Chain broken at seq {i} (entry {entry.id}): prev_hash mismatch"
            if _hash(expected, _canonical(entry.canonical_fields())) != entry.hash:
                return False, f"Chain broken at seq {i} (entry {entry.id})"
            expected = entry.hash
        n = len(self._entries)
        return True, f"Chain intact ({n} {'entry' if n == 1 else 'entries'})"

    def entries(self, after_seq: int = -1) -> list[LedgerEntry]:
        self.refresh()
        return self._entries[after_seq + 1 :]

    def query(
        self,
        actor: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[LedgerEntry]:
        self.refresh()
        results = self._entries
        if actor:
            results = [e for e in results if e.actor == actor]
        if action:
            results = [e for e in results if e.action == action]
        if since:
            results = [e for e in results if e.ts >= since]
        return results[-limit:]

    @property
    def head_hash(self) -> str:
        self.refresh()
        return self._head_hash

    def __len__(self) -> int:
        self.refresh()
        return len(self._entries)
