"""Tests for the Claude Code integration (templates, install, context hook)."""

import json
from pathlib import Path

import pytest
import yaml

from nazareth.claude_code import (
    CONTEXT_BUDGET,
    HOOK_COMMAND,
    ITEM_CHARS,
    LENSES,
    _template,
    install,
    path_warning,
    render_context,
)
from nazareth.cli import main
from nazareth.memory.service import HUMAN, Memory, NazarethError
from nazareth.paths import StatePaths


def _frontmatter(text: str) -> dict:
    _, fm, body = text.split("---\n", 2)
    assert body.strip()
    return yaml.safe_load(fm)


@pytest.mark.parametrize("lens", LENSES)
def test_lens_templates_are_well_formed(lens: str) -> None:
    fm = _frontmatter(_template(f"{lens}.md"))
    assert fm["name"] == lens
    assert fm["description"]
    # Each lens writes through its own server with a fixed identity, and can't
    # fall back to the main session's server (which would record the wrong actor).
    assert fm["disallowedTools"] == "mcp__nazareth__*"
    [server] = fm["mcpServers"]
    assert list(server) == ["lens-memory"]
    cfg = server["lens-memory"]
    assert cfg["command"] == "nazareth" and cfg["args"] == ["serve"]
    assert cfg["env"]["NAZARETH_ACTOR"] == "lens:" + lens.removeprefix("lens-")
    if "tools" in fm:
        assert "mcp__lens-memory__*" in fm["tools"]


@pytest.mark.parametrize("lens", ["lens-architect", "lens-reviewer"])
def test_read_only_lenses_cannot_edit(lens: str) -> None:
    tools = _frontmatter(_template(f"{lens}.md"))["tools"]
    assert "Edit" not in tools and "Write" not in tools


def test_install_creates_everything(tmp_path: Path) -> None:
    report = dict(install(tmp_path))
    for lens in LENSES:
        assert (tmp_path / ".claude" / "agents" / f"{lens}.md").exists()
    mcp = json.loads((tmp_path / ".mcp.json").read_text())
    assert mcp["mcpServers"]["nazareth"]["env"]["NAZARETH_ACTOR"] == "agent:main"
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    [group] = settings["hooks"]["SessionStart"]
    assert group["hooks"] == [{"type": "command", "command": HOOK_COMMAND}]
    assert "compact" in group["matcher"]
    assert report[".mcp.json"] == "added nazareth server"


def test_install_preserves_existing_config(tmp_path: Path) -> None:
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    mine = tmp_path / ".claude" / "agents" / "lens-code.md"
    mine.write_text("my own lens")
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    (tmp_path / ".claude" / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"SessionStart": [{"matcher": "startup",
                                    "hooks": [{"type": "command", "command": "echo hi"}]}]},
    }))

    install(tmp_path)
    install(tmp_path)  # idempotent

    assert mine.read_text() == "my own lens"
    servers = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert set(servers) == {"other", "nazareth"}
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    commands = [h["command"] for g in settings["hooks"]["SessionStart"] for h in g["hooks"]]
    assert commands == ["echo hi", HOOK_COMMAND]


def test_init_claude_via_cli(tmp_path: Path, capsys) -> None:
    main(["--dir", str(tmp_path), "init", "--claude"])
    out = capsys.readouterr().out
    assert "lens-architect.md: created" in out
    assert (tmp_path / ".nazareth").is_dir()
    assert (tmp_path / ".mcp.json").exists()


def test_context_silent_when_uninitialized(tmp_path: Path, capsys) -> None:
    main(["--dir", str(tmp_path), "context"])
    assert capsys.readouterr().out == ""
    assert not (tmp_path / ".nazareth").exists()


def test_context_snapshot(tmp_path: Path, capsys) -> None:
    paths = StatePaths(tmp_path)
    paths.init()
    mem = Memory(paths)
    c = mem.constrain("Python only", "skills", actor=HUMAN)
    a = mem.decide("storage", "SQLite", "simple", actor="lens:architect")
    mem.decide("storage", "Postgres", "scale", actor="lens:code")  # pending
    q = mem.ask("Windows?", actor="lens:code")
    mem.answer(q["id"], "no")
    mem.close()

    main(["--dir", str(tmp_path), "context"])
    out = capsys.readouterr().out
    assert c["id"] in out and a["id"] in out
    assert "1 pending decision" in out
    assert "Windows? → no" in out
    assert out.index("Constraints") < out.index("Active decisions")


def test_context_respects_budget() -> None:
    decisions = [{"id": f"dec-{i:06x}", "topic": f"topic-{i}", "text": "x" * 150}
                 for i in range(200)]
    brief = {"phase": {"current": "mvp", "next": "harden"}, "constraints": [],
             "decisions": decisions[:100], "active_decision_count": 200}
    text = render_context(brief)
    assert len(text) <= CONTEXT_BUDGET
    assert "more; use `query`" in text
    shown = text.count("- dec-")
    assert f"… {200 - shown} more" in text


def test_context_keeps_warning_and_instructions_when_constraints_overflow() -> None:
    constraints = [{"id": f"con-{i:06x}", "text": "c" * 300} for i in range(40)]
    decisions = [{"id": f"dec-{i:06x}", "topic": "t", "text": "d" * 100} for i in range(5)]
    brief = {"phase": {"current": "mvp", "next": "harden"}, "constraints": constraints,
             "decisions": decisions, "active_decision_count": 5,
             "pending_review": [{}], "open_questions": [],
             "recent_answers": [{"id": "q-1", "question": "Windows?", "answer": "no"}]}
    text = render_context(brief)
    assert len(text) <= CONTEXT_BUDGET
    assert "Don't rely on pending decisions." in text
    assert text.rstrip().endswith("lens-reviewer.")
    shown = text.count("- con-")
    assert 0 < shown < 40
    assert f"… {40 - shown} more; use `query`" in text
    assert "**Active decisions:**" in text  # every section keeps its title and pointer


def test_context_clips_long_items() -> None:
    brief = {"phase": {"current": "mvp", "next": "harden"},
             "constraints": [{"id": "con-1", "text": "x" * 20000}],
             "decisions": [], "active_decision_count": 0}
    line = next(ln for ln in render_context(brief).splitlines() if ln.startswith("- con-1"))
    assert len(line) == ITEM_CHARS and line.endswith("…")


def test_context_puts_waiting_before_answers_without_constraints() -> None:
    brief = {"phase": {"current": "mvp", "next": "harden"}, "constraints": [],
             "decisions": [], "active_decision_count": 0, "open_questions": [{}],
             "recent_answers": [{"id": "q-1", "question": "Windows?", "answer": "no"}]}
    text = render_context(brief)
    assert text.index("Waiting on the human") < text.index("Recently answered")


def test_context_reports_unreadable_memory(tmp_path: Path, capsys) -> None:
    paths = StatePaths(tmp_path)
    paths.init()
    Memory(paths).close()
    with paths.ledger.open("a") as f:
        f.write("not json\n")
    main(["--dir", str(tmp_path), "context"])
    out = capsys.readouterr().out
    assert "Unavailable" in out and "nazareth verify" in out


@pytest.mark.parametrize("rel, text", [
    (".mcp.json", "{not json"),
    (".mcp.json", "[]"),
    (".mcp.json", '{"mcpServers": []}'),
    (".claude/settings.json", '{"hooks": []}'),
    (".claude/settings.json", '{"hooks": {"SessionStart": [{"hooks": "x"}]}}'),
])
def test_init_rejects_bad_config_before_writing(tmp_path: Path, capsys, rel: str,
                                                text: str) -> None:
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(text)
    with pytest.raises(NazarethError, match="Nothing was changed"):
        install(tmp_path)
    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), "init", "--claude"])
    assert rel in capsys.readouterr().err
    assert (tmp_path / rel).read_text() == text
    assert not (tmp_path / ".nazareth").exists()
    assert not (tmp_path / ".claude" / "agents").exists()
    assert not list(tmp_path.rglob("*.nazareth-tmp"))


def test_path_warning(monkeypatch, tmp_path: Path) -> None:
    mine = tmp_path / "venv" / "nazareth"
    other = tmp_path / "global" / "nazareth"
    for script in (mine, other):
        script.parent.mkdir()
        script.write_text("")
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert "isn't on PATH" in path_warning(str(mine))
    monkeypatch.setattr("shutil.which", lambda _: str(other))
    assert str(other) in path_warning(str(mine))
    assert path_warning(str(other)) is None
    assert path_warning("/usr/lib/python3/runpy.py") is None  # not run as the script
