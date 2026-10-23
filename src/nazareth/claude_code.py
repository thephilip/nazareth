"""Claude Code integration: lens subagents, MCP registration, and a
SessionStart hook that puts the project's memory in context.

Nothing here runs unless the human asks for it with `nazareth init --claude`.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Any

from nazareth import workflows
from nazareth.ledger.chain import LedgerEntry
from nazareth.memory.service import COMMIT_ACTION, GIT_ACTOR, HUMAN, NazarethError

CONTEXT_BUDGET = 9000  # Claude Code caps injected hook output at 10,000 chars
LENSES = ("lens-architect", "lens-code", "lens-reviewer")
MAIN_ACTOR = "agent:main"
HOOK_COMMAND = "nazareth context"
HOOK_MATCHER = "startup|resume|clear|compact"
NUDGE_COMMAND = "nazareth nudge"
HOOKS = (  # (event, matcher, command)
    ("SessionStart", HOOK_MATCHER, HOOK_COMMAND),
    ("PostToolUse", "Bash", NUDGE_COMMAND),
)
# `commit` as git's subcommand, after any global options (`git -C dir commit`)
COMMIT_RE = re.compile(r"\bgit(?:\s+-[cC]\s+\S+|\s+-\S+)*\s+commit\b")
NUDGE_WINDOW = 2 * 3600  # without logged commits, look back this many seconds
NUDGE = (
    "nazareth: a commit just landed and nothing has been recorded in project memory since "
    "the previous one. If this work settled a design choice, record it now with the nazareth "
    "`decide` tool (one decision per choice, with the why), and `log` findings or follow-ups "
    "another agent would need. If nothing was decided, carry on."
)
# CLI commands that write as the human. Agents are denied them in the shell;
# they write through their own memory server, which never accepts `human`.
HUMAN_COMMANDS = ("decide", "constrain", "answer", "resolve", "retire", "phase", "log")
DENY_RULES = tuple(f"Bash(nazareth {c}:*)" for c in HUMAN_COMMANDS)
# The memory servers' tools (the main session's and every lens's), approved up
# front: auto mode's classifier otherwise refuses writes. They only append, under
# the server's fixed identity, and conflicts are held as pending for the human.
ALLOW_RULES = ("mcp__nazareth", "mcp__lens-memory")
_JSON_KINDS = {dict: "object", list: "array"}
ITEM_CHARS = 400  # longer items are clipped; `query` has the full text
MORE_CHARS = 48  # room for "- … N more; use `query`."


def _clip(text: str) -> str:
    return text if len(text) <= ITEM_CHARS else text[: ITEM_CHARS - 1] + "…"


def render_context(brief: dict[str, Any], lenses: tuple[str, ...] = LENSES) -> str:
    """A compact, agent-facing snapshot of project memory for session start.
    `lenses` are the lens subagents this project has.

    The header, the pending-work warning, next steps and the closing instructions always fit;
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
        "Record as you work, not at the end: `decide` each design choice once it's settled "
        "(change one by passing its id in `supersedes`), `ask` when you need the human, and "
        "`log` findings and follow-ups. If the human asks you to record a session, go back "
        "over its choices and do the same. "
        + (f"Specialist subagents: {', '.join(lenses)}. " if lenses else "")
        + "Suggest the human's workflows when they're due: `/nazareth-plan` before building "
        "from specs, `/nazareth-review` before pushing, `/nazareth-wrapup` at the end of a "
        "session.",
    ]
    waiting: list[str] = []
    pending, open_q = brief.get("pending_review", []), brief.get("open_questions", [])
    if pending or open_q:
        waiting = ["", (f"**Waiting on the human:** {len(pending)} pending decision(s), "
                        f"{len(open_q)} open question(s). Don't rely on pending decisions.")]
    if steps := brief.get("next_steps"):
        waiting += ["", "**Next steps to suggest to the human** (early, briefly):",
                    *(_clip(f"- {s['text']} `{s['command']}`") for s in steps)]

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


def nudge(events: list[LedgerEntry], command: str, now: float | None = None) -> str | None:
    """After a `git commit`, remind the agent to record decisions, unless an
    agent has written to memory since the previous commit."""
    if not COMMIT_RE.search(command):
        return None
    commits = [i for i, e in enumerate(events)
               if e.actor == GIT_ACTOR and e.action == COMMIT_ACTION]
    if commits and commits[-1] == len(events) - 1:
        commits.pop()  # the post-commit hook just logged this one; look back to the last
    if commits:
        since = events[commits[-1] + 1:]
    else:
        cutoff = (now or time.time()) - NUDGE_WINDOW
        since = [e for e in events if e.ts >= cutoff]
    if any(e.actor not in (HUMAN, GIT_ACTOR) for e in since):
        return None
    return NUDGE


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


def installed_lenses(project: Path) -> tuple[str, ...]:
    return tuple(lens for lens in LENSES
                 if (project / ".claude" / "agents" / f"{lens}.md").exists())


def install(project: Path, check_only: bool = False,
            lenses: tuple[str, ...] = LENSES) -> list[tuple[str, str]]:
    """Add lens subagents (`lenses`), the workflow skills, the MCP server, the
    context and commit-reminder hooks, allow rules for the memory tools and deny
    rules for the CLI's human write commands to a project. Existing files and
    entries are never overwritten, and existing config is checked before anything
    is written; `check_only` stops there.
    Returns (path, what happened) pairs."""
    mcp_path = project / ".mcp.json"
    mcp = _load_json(mcp_path, ".mcp.json")
    servers = _expect(mcp.setdefault("mcpServers", {}), dict, ".mcp.json", '"mcpServers"')

    settings_rel = ".claude/settings.json"
    settings_path = project / settings_rel
    settings = _load_json(settings_path, settings_rel)
    hooks = _expect(settings.setdefault("hooks", {}), dict, settings_rel, '"hooks"')
    for event, _, _ in HOOKS:
        for group in _expect(hooks.setdefault(event, []), list, settings_rel,
                             f'"hooks.{event}"'):
            _expect(group, dict, settings_rel, f"each {event} entry")
            for h in _expect(group.get("hooks", []), list, settings_rel,
                             'each entry\'s "hooks"'):
                _expect(h, dict, settings_rel, "each hook")
    permissions = _expect(settings.setdefault("permissions", {}), dict, settings_rel,
                          '"permissions"')
    allow = _expect(permissions.setdefault("allow", []), list, settings_rel,
                    '"permissions.allow"')
    deny = _expect(permissions.setdefault("deny", []), list, settings_rel,
                   '"permissions.deny"')
    if check_only:
        return []

    report: list[tuple[str, str]] = []
    files = [(project / ".claude" / "agents" / f"{lens}.md", _agent_file(lens))
             for lens in lenses]
    files += [(project / ".claude" / "skills" / f"nazareth-{name}" / "SKILL.md",
               workflows.skill_file(name)) for name in workflows.WORKFLOWS]
    for path, text in files:
        rel = str(path.relative_to(project))
        if path.exists():
            report.append((rel, "exists, left as is"))
        else:
            _write(path, text)
            report.append((rel, "created"))

    if "nazareth" in servers:
        report.append((".mcp.json", "already has a nazareth server, left as is"))
    else:
        servers["nazareth"] = {"command": "nazareth", "args": ["serve"],
                               "env": {"NAZARETH_ACTOR": MAIN_ACTOR}}
        _write(mcp_path, json.dumps(mcp, indent=2) + "\n")
        report.append((".mcp.json", "added nazareth server"))

    changes = []
    for event, matcher, command in HOOKS:
        if not any(h.get("command") == command
                   for group in hooks[event] for h in group.get("hooks", [])):
            hooks[event].append({"matcher": matcher,
                                 "hooks": [{"type": "command", "command": command}]})
            changes.append(f"{event} hook: {command}")
    missing = [r for r in ALLOW_RULES if r not in allow]
    if missing:
        allow.extend(missing)
        changes.append("allow rules for the memory tools")
    missing = [r for r in DENY_RULES if r not in deny]
    if missing:
        deny.extend(missing)
        changes.append("deny rules for the CLI's human write commands")
    if changes:
        _write(settings_path, json.dumps(settings, indent=2) + "\n")
        report.append((settings_rel, "added " + ", ".join(changes)))
    else:
        report.append((settings_rel, "already has the hooks and permission rules, left as is"))

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
