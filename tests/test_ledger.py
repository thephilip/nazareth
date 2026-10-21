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


def _append_many(path: str, actor: str, n: int, barrier) -> None:
    ledger = Ledger(Path(path))
    barrier.wait()  # all writers open the ledger first, then race
    for i in range(n):
        ledger.append(actor=actor, action="log", summary=f"{actor} event {i}")


def test_concurrent_processes_keep_chain_intact(tmp_path: Path) -> None:
    import multiprocessing as mp

    path = tmp_path / "shared.jsonl"
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(4)
    procs = [
        ctx.Process(target=_append_many, args=(str(path), f"lens:{i}", 40, barrier))
        for i in range(4)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0

    ledger = Ledger(path)
    assert len(ledger) == 160
    valid, msg = ledger.verify()
    assert valid, msg
    assert [e.seq for e in ledger.entries()] == list(range(160))


def test_stale_instance_appends_after_other_writer(tmp_path: Path) -> None:
    path = tmp_path / "two.jsonl"
    a, b = Ledger(path), Ledger(path)
    a.append(actor="lens:a", action="log", summary="from a")
    b.append(actor="lens:b", action="log", summary="from b")  # b never saw a's entry before this
    a.append(actor="lens:a", action="log", summary="from a again")

    assert len(a) == 3 and len(b) == 3
    valid, msg = Ledger(path).verify()
    assert valid, msg


def test_partial_trailing_line_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "partial.jsonl"
    Ledger(path).append(actor="human", action="log", summary="complete")
    with open(path, "a") as f:
        f.write('{"id":"evt_half')  # a writer mid-append

    reader = Ledger(path)
    assert len(reader) == 1


def test_reordered_entries_detected(tmp_path: Path) -> None:
    path = tmp_path / "reorder.jsonl"
    ledger = Ledger(path)
    for i in range(3):
        ledger.append(actor="human", action="log", summary=f"event {i}")
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n")

    valid, _ = Ledger(path).verify()
    assert not valid
