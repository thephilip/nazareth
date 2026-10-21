"""Tests for the hash-chain ledger."""

from pathlib import Path

import pytest

from nazareth.ledger.chain import Ledger


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "test_events.jsonl")


def test_empty_ledger_verifies(ledger: Ledger) -> None:
    valid, msg = ledger.verify()
    assert valid
    assert "0 entries" in msg


def test_append_and_verify(ledger: Ledger) -> None:
    ledger.append(actor="lens:architect", action="decision", summary="Use JWT for auth")
    ledger.append(actor="lens:code", action="artifact", summary="Created auth module", ref="src/auth.py")
    valid, msg = ledger.verify()
    assert valid
    assert "2 entries" in msg


def test_chain_persists_and_reloads(tmp_path: Path) -> None:
    path = tmp_path / "persist.jsonl"
    ledger1 = Ledger(path)
    ledger1.append(actor="human", action="directive", summary="Focus on auth first")
    ledger1.append(actor="lens:architect", action="decision", summary="Use API keys")

    ledger2 = Ledger(path)
    assert len(ledger2) == 2
    valid, _ = ledger2.verify()
    assert valid


def test_tamper_detection(tmp_path: Path) -> None:
    path = tmp_path / "tamper.jsonl"
    ledger = Ledger(path)
    ledger.append(actor="human", action="decision", summary="Original decision")
    ledger.append(actor="human", action="decision", summary="Second decision")

    lines = path.read_text().splitlines()
    import json
    entry = json.loads(lines[0])
    entry["summary"] = "Tampered decision"
    lines[0] = json.dumps(entry, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")

    reloaded = Ledger(path)
    valid, msg = reloaded.verify()
    assert not valid
    assert "Chain broken" in msg


def test_query_filters(ledger: Ledger) -> None:
    ledger.append(actor="lens:architect", action="decision", summary="Decision 1")
    ledger.append(actor="lens:code", action="artifact", summary="Artifact 1")
    ledger.append(actor="lens:architect", action="decision", summary="Decision 2")

    results = ledger.query(actor="lens:architect")
    assert len(results) == 2
    assert all(e.actor == "lens:architect" for e in results)

    results = ledger.query(action="artifact")
    assert len(results) == 1
