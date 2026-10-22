"""Tests for commit logging (the post-commit hook) and the commit nudge."""

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from nazareth import git_hook
from nazareth.claude_code import NUDGE
from nazareth.cli import main
from nazareth.memory.service import Memory
from nazareth.paths import StatePaths


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with isolated config, and this environment's `nazareth` first on PATH
    so the installed hook runs the code under test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.com")
    monkeypatch.setenv("PATH", str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.delenv("NAZARETH_PROJECT_DIR", raising=False)
    git(tmp_path, "init", "-q")
    return tmp_path


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout


def commit(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)


def commits(repo: Path) -> list[tuple[str, str]]:
    mem = Memory(StatePaths(repo))
    out = [(e.ref, e.summary) for e in mem.ledger.query(actor="git", action="commit")]
    mem.close()
    return out


def test_init_installs_a_hook_that_logs_commits(repo: Path, capsys) -> None:
    main(["--dir", str(repo), "init"])
    assert "post-commit: created" in capsys.readouterr().out
    hook = repo / ".git" / "hooks" / "post-commit"
    assert os.access(hook, os.X_OK)

    commit(repo, "a.py", "Add the alert poller")
    sha = git(repo, "rev-parse", "HEAD").strip()
    assert commits(repo) == [(sha[:10], "Add the alert poller (a.py)")]

    main(["--dir", str(repo), "init"])  # idempotent
    assert "already logs commits" in capsys.readouterr().out
    main(["--dir", str(repo), "record-commit"])  # a commit is logged once
    assert len(commits(repo)) == 1


def test_record_lists_a_few_files(repo: Path) -> None:
    StatePaths(repo).init()
    for i in range(7):
        (repo / f"f{i}.txt").write_text("x")
    git(repo, "add", *(f"f{i}.txt" for i in range(7)))
    git(repo, "commit", "-q", "-m", "Many files")
    mem = Memory(StatePaths(repo))
    out = git_hook.record(mem, repo)
    mem.close()
    assert out is not None
    [(_, summary)] = commits(repo)
    assert summary.startswith("Many files (f0.txt, ") and summary.endswith("… 2 more)")


def test_hook_is_harmless_without_nazareth_state(repo: Path) -> None:
    git_hook.install(repo)
    commit(repo, "a.py", "First")  # the hook runs, finds no .nazareth/, and does nothing
    assert not (repo / ".nazareth").exists()


def test_existing_hooks_are_left_alone(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text("#!/bin/sh\necho mine\n")
    [(_, what)] = git_hook.install(repo)
    assert "left as is" in what and git_hook.HOOK_MARKER in what
    assert hook.read_text() == "#!/bin/sh\necho mine\n"

    git(repo, "config", "core.hooksPath", ".husky")
    [(_, what)] = git_hook.install(repo)
    assert "core.hooksPath" not in what and "no git hook" in what
    assert not (repo / ".husky").exists()


def test_no_hook_outside_git(tmp_path: Path) -> None:
    assert git_hook.install(tmp_path) == []


def nudge_cli(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, command: str) -> str:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    main(["--dir", str(repo), "nudge"])
    return capsys.readouterr().out


def test_nudge_hook_output(repo: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    main(["--dir", str(repo), "init"])
    capsys.readouterr()
    commit(repo, "a.py", "First")
    out = json.loads(nudge_cli(repo, monkeypatch, capsys, "git commit -m First"))
    assert out == {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                          "additionalContext": NUDGE}}

    Memory(StatePaths(repo)).log("finding", "Poller needs backoff", actor="agent:main")
    commit(repo, "b.py", "Second")
    assert nudge_cli(repo, monkeypatch, capsys, "git commit -m Second") == ""
    assert nudge_cli(repo, monkeypatch, capsys, "ls") == ""

    monkeypatch.setattr(sys, "stdin", io.StringIO("not json"))
    main(["--dir", str(repo), "nudge"])
    assert capsys.readouterr().out == ""
