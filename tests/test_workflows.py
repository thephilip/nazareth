"""Tests for the workflows (plan, review, wrapup), next steps, and lens selection."""

import json
from pathlib import Path

import pytest
import yaml

from nazareth import workflows
from nazareth.claude_code import LENSES, install, installed_lenses, render_context
from nazareth.cli import main
from nazareth.memory.service import (
    COMMIT_ACTION,
    GIT_ACTOR,
    GRAPH_BEHIND_EVENTS,
    HUMAN,
    REVIEW_DUE_COMMITS,
    STALE_PHASE_COMMITS,
    Memory,
)
from nazareth.paths import StatePaths


@pytest.fixture
def paths(tmp_path: Path) -> StatePaths:
    p = StatePaths(tmp_path)
    p.init()
    return p


def commits(mem: Memory, n: int) -> None:
    for i in range(n):
        mem.ledger.append(actor=GIT_ACTOR, action=COMMIT_ACTION, summary=f"commit {i}",
                          ref=f"{i:010x}")


@pytest.mark.parametrize("name", sorted(workflows.WORKFLOWS))
def test_workflows_render_with_and_without_focus(name: str) -> None:
    default = workflows.render(name)
    assert workflows.WORKFLOWS[name].default_focus in default and "$" not in default
    assert "Focus: docs/spec/X.md" in workflows.render(name, " docs/spec/X.md ")
    # Host-neutral: lenses are optional, tools are nazareth's own.
    assert "brief" in default and "subagent" in default or name == "wrapup"


@pytest.mark.parametrize("name", sorted(workflows.WORKFLOWS))
def test_skill_files_are_valid(name: str) -> None:
    _, front, body = workflows.skill_file(name).split("---\n", 2)
    meta = yaml.safe_load(front)
    assert meta["name"] == f"nazareth-{name}"
    assert meta["description"] == workflows.WORKFLOWS[name].description
    assert "$ARGUMENTS" in body


def test_prompt_cli(paths: StatePaths, capsys) -> None:
    main(["--dir", str(paths.project), "prompt", "review", "src/app.ts", "src/lib.ts"])
    assert "Focus: src/app.ts src/lib.ts" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["prompt", "deploy"])


def test_next_steps_stale_phase_and_review(paths: StatePaths) -> None:
    mem = Memory(paths)
    commits(mem, REVIEW_DUE_COMMITS - 1)
    assert mem.next_steps() == []
    commits(mem, 1)
    [review] = mem.next_steps()
    assert review["command"] == "nazareth prompt review"
    mem.log("review", "Checked the paging change", actor="lens:reviewer")
    assert mem.next_steps() == []

    commits(mem, STALE_PHASE_COMMITS)
    texts = [s["text"] for s in mem.next_steps()]
    assert any("still `brainstorm` after" in t for t in texts)
    mem.set_phase(HUMAN, current="views-tuning")
    mem.log("finding", "Off-by-one in paging", actor="lens:reviewer", severity="low")
    assert mem.next_steps() == []
    assert "next_steps" not in mem.brief()
    commits(mem, REVIEW_DUE_COMMITS)
    assert mem.brief()["next_steps"][0]["text"].startswith(f"{REVIEW_DUE_COMMITS} commits")
    mem.close()


def test_next_steps_graph_behind(paths: StatePaths) -> None:
    mem = Memory(paths)
    for i in range(GRAPH_BEHIND_EVENTS + 1):
        mem.log("artifact", f"thing {i}", actor="agent:main")
    assert mem.next_steps() == []  # no graph: nothing to suggest
    paths.graph.touch()
    paths.graph_mark.write_text("0\n")
    [step] = mem.next_steps()
    assert step["text"].startswith(f"{GRAPH_BEHIND_EVENTS} ledger events")
    paths.graph_mark.write_text(f"{GRAPH_BEHIND_EVENTS}\n")
    assert mem.next_steps() == []
    paths.graph_mark.write_text("garbage")
    assert mem.next_steps() == []
    mem.close()


def test_status_and_context_show_next_steps(paths: StatePaths, capsys) -> None:
    mem = Memory(paths)
    commits(mem, REVIEW_DUE_COMMITS)
    brief = mem.brief()
    mem.close()
    main(["--dir", str(paths.project), "status"])
    out = capsys.readouterr().out
    assert "NEXT STEPS" in out and "nazareth prompt review" in out
    text = render_context(brief, ("lens-reviewer",))
    assert "Next steps to suggest to the human" in text and "nazareth prompt review" in text
    assert "Specialist subagents: lens-reviewer." in text


def test_install_lenses_subset_and_skills(tmp_path: Path, capsys) -> None:
    main(["--dir", str(tmp_path), "init", "--claude", "--lenses", "architect,reviewer"])
    assert installed_lenses(tmp_path) == ("lens-architect", "lens-reviewer")
    for name in workflows.WORKFLOWS:
        assert (tmp_path / ".claude" / "skills" / f"nazareth-{name}" / "SKILL.md").exists()
    capsys.readouterr()
    main(["--dir", str(tmp_path), "context"])
    assert "Specialist subagents: lens-architect, lens-reviewer." in capsys.readouterr().out
    install(tmp_path)  # a later full install adds only what's missing
    assert installed_lenses(tmp_path) == LENSES


@pytest.mark.parametrize(("argv", "message"), [
    (["init", "--lenses", "architect"], "use it with --claude"),
    (["init", "--claude", "--lenses", "architect,tester"], "unknown lens lens-tester"),
])
def test_bad_lenses(tmp_path: Path, capsys, argv: list[str], message: str) -> None:
    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), *argv])
    assert message in capsys.readouterr().err
    assert not (tmp_path / ".nazareth").exists() and not (tmp_path / ".claude").exists()


def test_settings_unchanged_by_lens_choice(tmp_path: Path) -> None:
    install(tmp_path, lenses=("lens-reviewer",))
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "PostToolUse" in settings["hooks"]
