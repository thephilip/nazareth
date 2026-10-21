"""Append-only hash-chained event ledger.

Ported from shem-spolia's event log. Each entry's hash covers the previous
hash plus the canonical form of the event, creating a tamper-evident chain.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
        self._entries: list[LedgerEntry] = []
        self._head_hash = hashlib.sha256(b"nazareth-genesis").hexdigest()
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with open(self._path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                entry = LedgerEntry(**raw)
                self._entries.append(entry)
                self._head_hash = entry.hash

    def append(
        self,
        actor: str,
        action: str,
        summary: str,
        ref: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEntry:
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
        new_hash = _hash(self._head_hash, canonical)
        entry = LedgerEntry(**{**asdict(entry), "hash": new_hash})
        self._head_hash = new_hash
        self._entries.append(entry)
        with open(self._path, "a") as f:
            f.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
        return entry

    def verify(self) -> tuple[bool, str]:
        """Walk the chain and verify every hash. Returns (valid, message)."""
        expected = hashlib.sha256(b"nazareth-genesis").hexdigest()
        for i, entry in enumerate(self._entries):
            canonical = _canonical(entry.canonical_fields())
            computed = _hash(expected, canonical)
            if computed != entry.hash:
                return False, f"Chain broken at seq {i} (entry {entry.id})"
            expected = entry.hash
        return True, f"Chain intact ({len(self._entries)} entries)"

    def query(
        self,
        actor: str | None = None,
        action: str | None = None,
        since: float | None = None,
        limit: int = 50,
    ) -> list[LedgerEntry]:
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
        return self._head_hash

    def __len__(self) -> int:
        return len(self._entries)
