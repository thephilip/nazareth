"""Tests for the Claude Code integration (templates, install, context hook)."""

import json
from pathlib import Path

import pytest
import yaml

from nazareth.claude_code import (
    CONTEXT_BUDGET,
    HOOK_COMMAND,
    LENSES,
    _template,
    install,
    render_context,
)
from nazareth.cli import main
from nazareth.memory.service import HUMAN, Memory
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
