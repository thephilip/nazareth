"""Workflows: prompts for the steps a human tends to forget (plan, review, wrapup).

Each is written once, host-neutrally, in `templates/workflows/<name>.md` and offered
four ways: as MCP prompts from `nazareth serve` (for hosts that list prompts), as
Claude Code skills installed by `init --claude`, as portable Agent Skills in
`.agents/skills/` (`init --skills`, read by Codex, OpenCode, Gemini CLI, pi and
others), and as text from `nazareth prompt` for any other agent.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from string import Template
from typing import NamedTuple


class Workflow(NamedTuple):
    description: str
    default_focus: str  # used when the human gives none


WORKFLOWS = {
    "plan": Workflow(
        description=("Plan work against the memory: read specs or a feature, record the "
                     "choices it settles, supersede the decisions it changes, and ask about "
                     "open points. Use when starting a feature or after drafting specs."),
        default_focus=("whatever the human describes next, or the specs and docs changed "
                       "since the last commit (check `git status` and `git log`)")),
    "review": Workflow(
        description=("Review the current change against recorded decisions and constraints "
                     "before it is pushed, logging findings with a severity."),
        default_focus=("uncommitted changes plus commits not yet pushed (`git diff`, "
                       "`git log @{u}..`)")),
    "wrapup": Workflow(
        description=("Wrap up a session: record decisions not yet recorded, log what was "
                     "built and found, and tell the human what waits on them."),
        default_focus="this session's work"),
}


def render(name: str, focus: str = "") -> str:
    if name not in WORKFLOWS:
        raise KeyError(name)
    text = resources.files("nazareth").joinpath("templates", "workflows", f"{name}.md")
    return Template(text.read_text()).safe_substitute(
        focus=focus.strip() or WORKFLOWS[name].default_focus)


def skill_file(name: str, portable: bool = False) -> str:
    """A skill (`/nazareth-<name>`) that runs the workflow. Claude Code's fills the
    focus from `$ARGUMENTS`; the `portable` one sticks to the Agent Skills fields and
    takes the focus from the request, since other hosts would show `$ARGUMENTS` as is."""
    description, default = WORKFLOWS[name]
    front = f"---\nname: nazareth-{name}\ndescription: {json.dumps(description)}\n"
    if portable:
        return front + "---\n\n" + render(
            name, f"what the human asked for along with this skill; if nothing, {default}")
    return (front + "argument-hint: [focus]\n---\n\n"
            + render(name, f"$ARGUMENTS\n\n(If no focus is given above: {default}.)"))


SKILLS_DIR = Path(".agents") / "skills"


def install_skills(project: Path) -> list[tuple[str, str]]:
    """Write the portable skills to `.agents/skills/`, leaving existing files alone.
    Returns (path, what happened) pairs."""
    report = []
    for name in WORKFLOWS:
        rel = SKILLS_DIR / f"nazareth-{name}" / "SKILL.md"
        path = project / rel
        if path.exists():
            report.append((str(rel), "exists, left as is"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(skill_file(name, portable=True))
            report.append((str(rel), "created"))
    return report
