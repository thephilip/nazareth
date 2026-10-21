"""Nazareth MCP memory server.

A thin MCP layer over `Memory`. Every state change is a ledger event, so the
ledger is the audit trail; read-only calls are not logged.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from nazareth.memory.service import HUMAN, Memory, NazarethError
from nazareth.paths import StatePaths, find_project_dir

INSTRUCTIONS = """\
Nazareth is this project's shared memory: decisions, constraints, open questions
and an append-only log, shared by every agent and the human.

- Start every task with `brief` to load the phase, constraints, active decisions
  and anything waiting on the human.
- Constraints are non-negotiable. Active decisions are binding until superseded.
- Record choices with `decide`. To change one, pass its id in `supersedes`; a
  conflicting decision that doesn't is held as pending for the human.
- When you need the human, `ask` instead of guessing or blocking.
- `log` what you produce (artifacts, findings, tests) so others can find it.
"""


def _actor(actor: str | None) -> str:
    """Who is writing. A fixed NAZARETH_ACTOR wins over the per-call argument,
    so a lens can't claim another identity; nobody may write as the human."""
    claimed = (actor or "").strip()
    fixed = (os.environ.get("NAZARETH_ACTOR") or "").strip()
    if HUMAN in (claimed.lower(), fixed.lower()):
        raise NazarethError(
            f"Agents can't write as {HUMAN!r}. The human records decisions through the "
            "`nazareth` CLI; leave `actor` unset or name yourself, e.g. 'lens:architect'."
        )
    return fixed or claimed or "agent"


def build_server(paths: StatePaths | None = None) -> MCPServer:
    paths = paths or StatePaths(find_project_dir())
    server = MCPServer("nazareth", instructions=INSTRUCTIONS)
    state: dict[str, Memory] = {}

    def call(fn: Callable[[Memory], Any]) -> Any:
        try:
            if "mem" not in state:
                state["mem"] = Memory(paths)
            return fn(state["mem"])
        except NazarethError as e:
            raise ToolError(str(e)) from e

    @server.tool()
    def brief() -> dict:
        """Orient yourself: current phase, all active constraints, all active
        decisions (id, topic, text), records pending human review, open and
        recently answered questions, and recent activity. Call this first."""
        return call(lambda m: m.brief())

    @server.tool()
    def query(text: str | None = None, id: str | None = None,
              include_inactive: bool = False, limit: int = 10) -> dict:
        """Look things up. Pass `id` to read one decision, constraint, question
        or event in full, including what it supersedes, what superseded it and
        what depends on it. Or pass `text` to keyword-search decisions,
        constraints, questions and logged events. Only records in effect (or
        pending) are returned unless include_inactive is true; superseded ones
        then carry `current`, the id now in effect."""
        if id:
            return call(lambda m: m.get(id))
        if text:
            return call(lambda m: m.search(text, include_inactive=include_inactive, limit=limit))
        raise ToolError("Pass `text` to search or `id` to read one item.")

    @server.tool()
    def decide(topic: str, decision: str, rationale: str,
               supersedes: list[str] | None = None, depends_on: list[str] | None = None,
               actor: str | None = None) -> dict:
        """Record a decision and why. `topic` is a short area like 'auth' or
        'storage'. To change an earlier decision, list its id in `supersedes`;
        without that, a decision on a topic that already has one is held as
        pending for the human. Superseding something already replaced fails
        and names the current one. `depends_on` lists ids this builds on.
        Returns the new id, its status, and related active records to check
        for overlap. `actor` names you (e.g. 'lens:architect') and is used only
        when the server has no fixed identity."""
        return call(lambda m: m.decide(topic, decision, rationale, _actor(actor),
                                       supersedes=supersedes, depends_on=depends_on))

    @server.tool()
    def constrain(constraint: str, reason: str, topic: str | None = None,
                  supersedes: list[str] | None = None, actor: str | None = None) -> dict:
        """Record a non-negotiable project constraint (e.g. 'No OAuth for MVP')
        that every agent must respect. To replace one, list its id in
        `supersedes`."""
        return call(lambda m: m.constrain(constraint, reason, _actor(actor), topic=topic,
                                          supersedes=supersedes))

    @server.tool()
    def ask(question: str, context: str | None = None, ref: str | None = None,
            actor: str | None = None) -> dict:
        """Leave a question for the human instead of guessing or blocking.
        They answer asynchronously; answers appear in `brief`. Include enough
        `context` to answer without reading the code; `ref` is a file or id."""
        return call(lambda m: m.ask(question, _actor(actor), context=context, ref=ref))

    @server.tool()
    def log(action: str, summary: str, ref: str | None = None, severity: str | None = None,
            actor: str | None = None) -> dict:
        """Append an event to the project ledger: what you produced or found.
        `action` is a short verb-noun like 'artifact', 'finding', 'test',
        'refactor'. `ref` is a file or resource (e.g. 'src/auth.py:23').
        `severity` (low/medium/high/critical) is for findings."""
        return call(lambda m: m.log(action, summary, _actor(actor), ref=ref, severity=severity))

    @server.tool()
    def phase(set_current: str | None = None, set_next: str | None = None,
              actor: str | None = None) -> dict:
        """Read the project phase (e.g. brainstorm, scaffold, mvp, harden), or
        change it by passing set_current and/or set_next. Respect it: don't
        gold-plate during MVP or cut corners while hardening."""
        if set_current is None and set_next is None:
            return call(lambda m: m.phase())
        return call(lambda m: m.set_phase(_actor(actor), current=set_current,
                                          next_phase=set_next))

    @server.tool()
    def history(since: str | None = None, actor: str | None = None,
                action: str | None = None, limit: int = 50) -> dict:
        """Read the ledger, newest last. Filter by `since` ('30m', '8h', '7d'
        or an ISO date), `actor`, and `action` (e.g. 'decision', 'finding')."""
        return call(lambda m: m.history(since=since, actor=actor, action=action, limit=limit))

    return server


def run_server() -> None:
    build_server().run("stdio")


if __name__ == "__main__":
    run_server()
