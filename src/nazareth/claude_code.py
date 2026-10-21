"""Claude Code integration: lens subagents, MCP registration, and a
SessionStart hook that puts the project's memory in context.

Nothing here runs unless the human asks for it with `nazareth init --claude`.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

CONTEXT_BUDGET = 9000  # Claude Code caps injected hook output at 10,000 chars
LENSES = ("lens-architect", "lens-code", "lens-reviewer")
MAIN_ACTOR = "agent:main"
HOOK_COMMAND = "nazareth context"
HOOK_MATCHER = "startup|resume|clear|compact"


def render_context(brief: dict[str, Any]) -> str:
    """A compact, agent-facing snapshot of project memory for session start."""
    ph = brief["phase"]
    head = [
        "## Project memory (nazareth)",
        (f"Phase: {ph['current']} (next: {ph['next']}). Snapshot from session start; the "
         "`nazareth` MCP tools have the live state."),
    ]
    tail = [
        "",
        "Record decisions with `decide` (change one by passing its id in `supersedes`), "
        "leave questions for the human with `ask`, and `log` artifacts and findings. "
        "Specialist subagents: " + ", ".join(LENSES) + ".",
    ]

    sections: list[list[str]] = []
    if brief["constraints"]:
        sections.append(["", "**Constraints (binding):**"]
                        + [f"- {r['id']}: {r['text']}" for r in brief["constraints"]])
    pending, open_q = brief.get("pending_review", []), brief.get("open_questions", [])
    if pending or open_q:
        sections.append(["", (f"**Waiting on the human:** {len(pending)} pending decision(s), "
                              f"{len(open_q)} open question(s). Don't rely on pending decisions.")])
    if brief.get("recent_answers"):
        sections.append(["", "**Recently answered:**"]
                        + [f"- {q['id']}: {q['question']} → {q['answer']}"
                           for q in brief["recent_answers"][:5]])

    decisions = [f"- {r['id']} [{r['topic']}]: {r['text']}" for r in brief["decisions"]]
    fixed = sum(len(line) + 1 for part in (head, tail, *sections) for line in part)
    shown: list[str] = []
    used = fixed + 40
    for line in decisions:
        if used + len(line) + 1 > CONTEXT_BUDGET:
            break
        shown.append(line)
        used += len(line) + 1
    if decisions:
        block = ["", "**Active decisions:**", *shown]
        hidden = brief["active_decision_count"] - len(shown)
        if hidden:
            block.append(f"- … {hidden} more; use `query`.")
        sections.insert(1 if brief["constraints"] else 0, block)

    text = "\n".join(head + [line for part in sections for line in part] + tail)
    return text[:CONTEXT_BUDGET]


def _template(name: str) -> str:
    return resources.files("nazareth").joinpath("templates", "claude", name).read_text()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    return json.loads(text) if text else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def install(project: Path) -> list[tuple[str, str]]:
    """Add lens subagents, the MCP server, and the SessionStart hook to a
    project. Existing files and entries are never overwritten. Returns
    (path, what happened) pairs."""
    report: list[tuple[str, str]] = []

    for lens in LENSES:
        path = project / ".claude" / "agents" / f"{lens}.md"
        rel = str(path.relative_to(project))
        if path.exists():
            report.append((rel, "exists, left as is"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_template(f"{lens}.md"))
            report.append((rel, "created"))

    mcp_path = project / ".mcp.json"
    mcp = _load_json(mcp_path)
    servers = mcp.setdefault("mcpServers", {})
    if "nazareth" in servers:
        report.append((".mcp.json", "already has a nazareth server, left as is"))
    else:
        servers["nazareth"] = {"command": "nazareth", "args": ["serve"],
                               "env": {"NAZARETH_ACTOR": MAIN_ACTOR}}
        _write_json(mcp_path, mcp)
        report.append((".mcp.json", "added nazareth server"))

    settings_path = project / ".claude" / "settings.json"
    settings = _load_json(settings_path)
    session_start = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
    already = any(h.get("command") == HOOK_COMMAND
                  for group in session_start for h in group.get("hooks", []))
    if already:
        report.append((".claude/settings.json", "already has the context hook, left as is"))
    else:
        session_start.append({"matcher": HOOK_MATCHER,
                              "hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        _write_json(settings_path, settings)
        report.append((".claude/settings.json", "added SessionStart hook: nazareth context"))

    return report
