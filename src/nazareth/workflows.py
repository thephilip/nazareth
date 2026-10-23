"""Workflows: prompts for the steps a human tends to forget (plan, review, wrapup).

Each is written once, host-neutrally, in `templates/workflows/<name>.md` and offered
three ways: as MCP prompts from `nazareth serve` (for hosts that list prompts), as
Claude Code skills installed by `init --claude`, and as text from `nazareth prompt`
for any other agent.
"""

from __future__ import annotations

import json
from importlib import resources
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


def skill_file(name: str) -> str:
    """A Claude Code skill (`/nazareth-<name>`) that runs the workflow."""
    description, default = WORKFLOWS[name]
    return (f"---\nname: nazareth-{name}\ndescription: {json.dumps(description)}\n"
            "argument-hint: [focus]\n---\n\n"
            + render(name, f"$ARGUMENTS\n\n(If no focus is given above: {default}.)"))
