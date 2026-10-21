"""Claude Code integration: lens subagents, MCP registration, and a
SessionStart hook that puts the project's memory in context.

Nothing here runs unless the human asks for it with `nazareth init --claude`.
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path
from typing import Any

from nazareth.memory.service import NazarethError

CONTEXT_BUDGET = 9000  # Claude Code caps injected hook output at 10,000 chars
LENSES = ("lens-architect", "lens-code", "lens-reviewer")
MAIN_ACTOR = "agent:main"
HOOK_COMMAND = "nazareth context"
HOOK_MATCHER = "startup|resume|clear|compact"
_JSON_KINDS = {dict: "object", list: "array"}
ITEM_CHARS = 400  # longer items are clipped; `query` has the full text
MORE_CHARS = 48  # room for "- … N more; use `query`."


def _clip(text: str) -> str:
    return text if len(text) <= ITEM_CHARS else text[: ITEM_CHARS - 1] + "…"


def render_context(brief: dict[str, Any]) -> str:
    """A compact, agent-facing snapshot of project memory for session start.

    The header, the pending-work warning and the closing instructions always fit;
    constraints, then decisions, then recent answers share what's left, each
    saying how many items it left out."""
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
    waiting: list[str] = []
    pending, open_q = brief.get("pending_review", []), brief.get("open_questions", [])
    if pending or open_q:
        waiting = ["", (f"**Waiting on the human:** {len(pending)} pending decision(s), "
                        f"{len(open_q)} open question(s). Don't rely on pending decisions.")]

    answers = brief.get("recent_answers", [])[:5]
    parts = [  # (title, lines, total, where to find the rest), in budget priority order
        ("**Constraints (binding):**",
         [_clip(f"- {r['id']}: {r['text']}") for r in brief["constraints"]],
         len(brief["constraints"]), "`query`"),
        ("**Active decisions:**",
         [_clip(f"- {r['id']} [{r['topic']}]: {r['text']}") for r in brief["decisions"]],
         brief["active_decision_count"], "`query`"),
        ("**Recently answered:**",
         [_clip(f"- {q['id']}: {q['question']} → {q['answer']}") for q in answers],
         len(answers), "`brief`"),
    ]
    parts = [part for part in parts if part[2]]

    def size(lines: list[str]) -> int:
        return sum(len(line) + 1 for line in lines)

    def frame(title: str) -> int:  # blank line, title and a "… N more" line
        return size(["", title]) + MORE_CHARS

    room = CONTEXT_BUDGET - size(head + waiting + tail) - sum(frame(t) for t, *_ in parts)
    blocks: dict[str, list[str]] = {}
    for title, lines, total, where in parts:
        shown: list[str] = []
        for line in lines:
            if len(line) + 1 > room:
                break
            shown.append(line)
            room -= len(line) + 1
        block = ["", title, *shown]
        if total > len(shown):
            block.append(f"- … {total - len(shown)} more; use {where}.")
        blocks[title] = block

    answered = blocks.pop("**Recently answered:**", [])
    body = [line for block in blocks.values() for line in block] + waiting + answered
    return "\n".join(head + body + tail)[:CONTEXT_BUDGET]


def template(*parts: str) -> str:
    return resources.files("nazareth").joinpath("templates", *parts).read_text()


def lens_body(lens: str) -> str:
    """The host-neutral prompt for a lens, shared by every host."""
    return template("lenses", f"{lens}.md")


def _agent_file(lens: str) -> str:
    return template("claude", f"{lens}.frontmatter") + lens_body(lens)


def _load_json(path: Path, rel: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text().strip()
    try:
        data = json.loads(text) if text else {}
    except json.JSONDecodeError as e:
        raise NazarethError(f"{rel} isn't valid JSON ({e}); fix it and rerun. "
                            "Nothing was changed.") from e
    if not isinstance(data, dict):
        raise NazarethError(f"{rel} should hold a JSON object; fix it and rerun. "
                            "Nothing was changed.")
    return data


def _expect(value: Any, kind: type, rel: str, where: str) -> Any:
    if not isinstance(value, kind):
        raise NazarethError(f"{rel}: {where} should be a JSON {_JSON_KINDS[kind]}; fix it and "
                            "rerun. Nothing was changed.")
    return value


def _write(path: Path, text: str) -> None:
    """Write via a temporary file and rename, so an interrupted run never
    leaves a truncated file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.nazareth-tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def install(project: Path, check_only: bool = False) -> list[tuple[str, str]]:
    """Add lens subagents, the MCP server, and the SessionStart hook to a
    project. Existing files and entries are never overwritten, and existing
    config is checked before anything is written; `check_only` stops there.
    Returns (path, what happened) pairs."""
    mcp_path = project / ".mcp.json"
    mcp = _load_json(mcp_path, ".mcp.json")
    servers = _expect(mcp.setdefault("mcpServers", {}), dict, ".mcp.json", '"mcpServers"')

    settings_rel = ".claude/settings.json"
    settings_path = project / settings_rel
    settings = _load_json(settings_path, settings_rel)
    hooks = _expect(settings.setdefault("hooks", {}), dict, settings_rel, '"hooks"')
    session_start = _expect(hooks.setdefault("SessionStart", []), list, settings_rel,
                            '"hooks.SessionStart"')
    for group in session_start:
        _expect(group, dict, settings_rel, "each SessionStart entry")
        for h in _expect(group.get("hooks", []), list, settings_rel, 'each entry\'s "hooks"'):
            _expect(h, dict, settings_rel, "each hook")
    if check_only:
        return []

    report: list[tuple[str, str]] = []
    for lens in LENSES:
        path = project / ".claude" / "agents" / f"{lens}.md"
        rel = str(path.relative_to(project))
        if path.exists():
            report.append((rel, "exists, left as is"))
        else:
            _write(path, _agent_file(lens))
            report.append((rel, "created"))

    if "nazareth" in servers:
        report.append((".mcp.json", "already has a nazareth server, left as is"))
    else:
        servers["nazareth"] = {"command": "nazareth", "args": ["serve"],
                               "env": {"NAZARETH_ACTOR": MAIN_ACTOR}}
        _write(mcp_path, json.dumps(mcp, indent=2) + "\n")
        report.append((".mcp.json", "added nazareth server"))

    already = any(h.get("command") == HOOK_COMMAND
                  for group in session_start for h in group.get("hooks", []))
    if already:
        report.append((settings_rel, "already has the context hook, left as is"))
    else:
        session_start.append({"matcher": HOOK_MATCHER,
                              "hooks": [{"type": "command", "command": HOOK_COMMAND}]})
        _write(settings_path, json.dumps(settings, indent=2) + "\n")
        report.append((settings_rel, "added SessionStart hook: nazareth context"))

    return report


def path_warning(running: str) -> str | None:
    """Claude Code and Docker Agent run the bare `nazareth` command for the
    hook and every MCP server. Say so if that's missing, or isn't the
    `running` script."""
    found = shutil.which("nazareth")
    if found is None:
        return ("`nazareth` isn't on PATH, so agent hosts can't run the context hook or start "
                "the lens memory servers. Install it globally, e.g. `uv tool install -e .` "
                "from the nazareth repo.")
    if Path(running).name == "nazareth" and Path(found).resolve() != Path(running).resolve():
        return (f"Agent hosts will run the `nazareth` on PATH ({found}), not this one "
                f"({running}); make sure that one is current.")
    return None
