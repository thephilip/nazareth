"""Tests for the human-facing CLI."""

import json
from pathlib import Path

import pytest

from nazareth.cli import main
from nazareth.memory.service import Memory
from nazareth.paths import StatePaths


def run(tmp_path: Path, capsys, *argv: str) -> str:
    main(["--dir", str(tmp_path), *argv])
    return capsys.readouterr().out


def test_commands_require_init(tmp_path: Path, capsys) -> None:
    for argv in (["status"], ["verify"]):
        with pytest.raises(SystemExit) as exc:
            main(["--dir", str(tmp_path), *argv])
        assert exc.value.code == 1
        assert "nazareth init" in capsys.readouterr().err
    assert not (tmp_path / ".nazareth").exists()


def test_status_shows_what_needs_the_human(tmp_path: Path, capsys) -> None:
    run(tmp_path, capsys, "init")
    mem = Memory(StatePaths(tmp_path))
    a = mem.decide("auth", "Use API keys", "MVP", actor="lens:architect")
    p = mem.decide("auth", "Use OAuth", "customers", actor="lens:code")
    q = mem.ask("Support Windows?", actor="lens:code", context="CI matrix")
    mem.close()

    out = run(tmp_path, capsys, "status")
    assert "NEEDS YOU (2)" in out
    assert p["id"] in out and q["id"] in out
    assert a["id"] in out
    assert "Chain intact" in out


def test_human_workflow(tmp_path: Path, capsys) -> None:
    run(tmp_path, capsys, "init")
    mem = Memory(StatePaths(tmp_path))
    mem.decide("auth", "Use API keys", "MVP", actor="lens:architect")
    p = mem.decide("auth", "Use OAuth", "customers", actor="lens:code")
    q = mem.ask("Support Windows?", actor="lens:code")
    mem.close()

    resolved = json.loads(run(tmp_path, capsys, "resolve", p["id"], "--accept", "--note", "ok"))
    assert resolved["status"] == "active"
    assert "answered" in run(tmp_path, capsys, "answer", q["id"], "No")
    con = json.loads(run(tmp_path, capsys, "constrain", "Python only", "--why", "skills"))
    assert "retired" in run(tmp_path, capsys, "retire", con["id"], "--why", "changed")
    assert "mvp" in run(tmp_path, capsys, "phase", "--current", "mvp")
    assert "dogfood" in run(tmp_path, capsys, "phase", "dogfood", "--next", "search")

    out = run(tmp_path, capsys, "status")
    assert "NEEDS YOU: nothing" in out
    assert run(tmp_path, capsys, "verify").startswith("Chain intact")
    assert "Rebuilt index" in run(tmp_path, capsys, "rebuild")
    history = run(tmp_path, capsys, "history", "--actor", "human")
    assert "resolution" in history and "answer" in history


def test_human_decide_and_query(tmp_path: Path, capsys) -> None:
    run(tmp_path, capsys, "init")
    d = json.loads(run(tmp_path, capsys, "decide", "storage", "Use SQLite", "--why", "simple"))
    found = json.loads(run(tmp_path, capsys, "query", "sqlite"))
    assert [r["id"] for r in found["records"]] == [d["id"]]
    one = json.loads(run(tmp_path, capsys, "query", "--id", d["id"]))
    assert one["actor"] == "human"


def test_verify_fails_on_tamper(tmp_path: Path, capsys) -> None:
    run(tmp_path, capsys, "init")
    run(tmp_path, capsys, "log", "directive", "Focus on auth")
    ledger = tmp_path / ".nazareth" / "ledger.jsonl"
    ledger.write_text(ledger.read_text().replace("Focus on auth", "Focus on billing"))
    with pytest.raises(SystemExit) as exc:
        run(tmp_path, capsys, "verify")
    assert exc.value.code == 1
