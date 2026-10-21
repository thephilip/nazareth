"""Nazareth MCP memory server.

Exposes 7 operations for agent lenses to read/write shared project state:
decide, constrain, query, log, supersede, phase, history.

Backed by Graphiti (temporal knowledge graph), a JSONL hash-chain ledger,
and LanceDB (vector search).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from nazareth.ledger.chain import Ledger
from nazareth.memory.graph import create_graph

PROJECT_DIR = Path(os.environ.get("NAZARETH_PROJECT_DIR", "."))
STATE_DIR = PROJECT_DIR / ".state"
LEDGER_PATH = STATE_DIR / "ledger" / "events.jsonl"
PHASE_PATH = STATE_DIR / "phase.md"


def _read_phase() -> dict[str, str]:
    if not PHASE_PATH.exists():
        return {"current": "brainstorm", "next": "scaffold"}
    lines = PHASE_PATH.read_text().strip().splitlines()
    result = {}
    for line in lines:
        if line.startswith("current:"):
            result["current"] = line.split(":", 1)[1].strip()
        elif line.startswith("next:"):
            result["next"] = line.split(":", 1)[1].strip()
    return result or {"current": "brainstorm", "next": "scaffold"}


def _write_phase(current: str, next_phase: str) -> None:
    PHASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PHASE_PATH.write_text(f"current: {current}\nnext: {next_phase}\n")


TOOLS = [
    Tool(
        name="decide",
        description=(
            "Record a project decision with rationale. Automatically supersedes "
            "prior decisions on the same topic via the temporal knowledge graph."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": "Decision domain (e.g. 'authentication', 'database', 'api-design')",
                },
                "decision": {
                    "type": "string",
                    "description": "What was decided",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this decision was made",
                },
                "actor": {
                    "type": "string",
                    "description": "Who made this decision (e.g. 'lens:architect', 'human')",
                },
            },
            "required": ["topic", "decision", "rationale", "actor"],
        },
    ),
    Tool(
        name="constrain",
        description=(
            "Add a non-negotiable project constraint that all lenses must respect."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "constraint": {
                    "type": "string",
                    "description": "The constraint (e.g. 'No OAuth for MVP')",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this constraint exists",
                },
                "actor": {
                    "type": "string",
                    "description": "Who set this constraint",
                },
            },
            "required": ["constraint", "reason", "actor"],
        },
    ),
    Tool(
        name="query",
        description=(
            "Query the project knowledge graph. Returns currently valid decisions, "
            "constraints, and context with dependency chains. Use natural language."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "Natural language question (e.g. 'What decisions affect auth?')",
                },
                "lens": {
                    "type": "string",
                    "description": "Which lens is asking (scopes the response)",
                },
            },
            "required": ["question"],
        },
    ),
    Tool(
        name="log",
        description=(
            "Append an event to the project ledger. Used for actions taken, "
            "artifacts produced, findings raised."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "Event type (e.g. 'artifact', 'finding', 'refactor', 'test')",
                },
                "summary": {
                    "type": "string",
                    "description": "What happened",
                },
                "ref": {
                    "type": "string",
                    "description": "File or resource reference (e.g. 'src/auth/api_key.ts:23')",
                },
                "actor": {
                    "type": "string",
                    "description": "Who did this",
                },
                "severity": {
                    "type": "string",
                    "description": "For findings: low, medium, high, critical",
                },
            },
            "required": ["action", "summary", "actor"],
        },
    ),
    Tool(
        name="supersede",
        description="Explicitly mark a decision as replaced by a newer one.",
        inputSchema={
            "type": "object",
            "properties": {
                "old_topic": {
                    "type": "string",
                    "description": "Topic of the decision being superseded",
                },
                "new_decision": {
                    "type": "string",
                    "description": "The replacement decision",
                },
                "reason": {
                    "type": "string",
                    "description": "Why it was superseded",
                },
                "actor": {
                    "type": "string",
                    "description": "Who made this change",
                },
            },
            "required": ["old_topic", "new_decision", "reason", "actor"],
        },
    ),
    Tool(
        name="phase",
        description="Get or set the current project phase.",
        inputSchema={
            "type": "object",
            "properties": {
                "set_current": {
                    "type": "string",
                    "description": "Set current phase (omit to just read)",
                },
                "set_next": {
                    "type": "string",
                    "description": "Set next phase (omit to just read)",
                },
                "actor": {
                    "type": "string",
                    "description": "Who changed the phase",
                },
            },
        },
    ),
    Tool(
        name="history",
        description="Query the project ledger for past events.",
        inputSchema={
            "type": "object",
            "properties": {
                "since": {
                    "type": "string",
                    "description": "Time window (e.g. '1h', '8h', '24h', '7d')",
                },
                "actor": {
                    "type": "string",
                    "description": "Filter by actor",
                },
                "action": {
                    "type": "string",
                    "description": "Filter by action type",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default 50)",
                },
            },
        },
    ),
]


def _parse_duration(s: str) -> float:
    """Convert duration string to seconds offset from now."""
    s = s.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    for suffix, mult in multipliers.items():
        if s.endswith(suffix):
            return time.time() - float(s[:-1]) * mult
    return 0.0


class NazarethServer:
    def __init__(self) -> None:
        self.server = Server("nazareth")
        self.ledger = Ledger(LEDGER_PATH)
        self.graphiti = None
        self._setup_handlers()

    def _setup_handlers(self) -> None:
        @self.server.list_tools()
        async def list_tools() -> list[Tool]:
            return TOOLS

        @self.server.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
            if self.graphiti is None:
                self.graphiti = await create_graph(
                    host=os.environ.get("FALKORDB_HOST", "localhost"),
                    port=int(os.environ.get("FALKORDB_PORT", "6379")),
                )

            handler = getattr(self, f"_handle_{name}", None)
            if handler is None:
                return [TextContent(type="text", text=f"Unknown tool: {name}")]

            result = await handler(arguments)

            self.ledger.append(
                actor=arguments.get("actor", "system"),
                action=f"mcp:{name}",
                summary=f"Tool call: {name}",
                payload={"args": arguments, "result_preview": str(result)[:200]},
            )

            return [TextContent(type="text", text=json.dumps(result, indent=2))]

    async def _handle_decide(self, args: dict) -> dict:
        episode_content = (
            f"Decision on {args['topic']}: {args['decision']}. "
            f"Rationale: {args['rationale']}"
        )
        await self.graphiti.add_episode(
            group_id="project",
            name=f"decision:{args['topic']}",
            episode_body=episode_content,
            source_description=f"Decision by {args['actor']}",
        )
        self.ledger.append(
            actor=args["actor"],
            action="decision",
            summary=args["decision"],
            payload={"topic": args["topic"], "rationale": args["rationale"]},
        )
        return {"status": "recorded", "topic": args["topic"]}

    async def _handle_constrain(self, args: dict) -> dict:
        episode_content = (
            f"Project constraint: {args['constraint']}. "
            f"Reason: {args['reason']}"
        )
        await self.graphiti.add_episode(
            group_id="project",
            name=f"constraint:{args['constraint'][:40]}",
            episode_body=episode_content,
            source_description=f"Constraint set by {args['actor']}",
        )
        self.ledger.append(
            actor=args["actor"],
            action="constraint",
            summary=args["constraint"],
            payload={"reason": args["reason"]},
        )
        return {"status": "recorded", "constraint": args["constraint"]}

    async def _handle_query(self, args: dict) -> dict:
        results = await self.graphiti.search(
            group_ids=["project"],
            query=args["question"],
            num_results=10,
        )
        formatted = []
        for edge in results:
            formatted.append({
                "fact": edge.fact,
                "source": edge.source_node_name if hasattr(edge, "source_node_name") else None,
                "target": edge.target_node_name if hasattr(edge, "target_node_name") else None,
                "valid_at": str(edge.valid_at) if hasattr(edge, "valid_at") else None,
                "invalid_at": str(edge.invalid_at) if hasattr(edge, "invalid_at") else None,
            })
        return {"results": formatted, "query": args["question"]}

    async def _handle_log(self, args: dict) -> dict:
        entry = self.ledger.append(
            actor=args["actor"],
            action=args["action"],
            summary=args["summary"],
            ref=args.get("ref"),
            payload={k: v for k, v in args.items() if k not in ("actor", "action", "summary", "ref")},
        )
        return {"status": "logged", "entry_id": entry.id}

    async def _handle_supersede(self, args: dict) -> dict:
        episode_content = (
            f"Superseding decision on {args['old_topic']}: "
            f"new decision is {args['new_decision']}. "
            f"Reason for change: {args['reason']}"
        )
        await self.graphiti.add_episode(
            group_id="project",
            name=f"supersede:{args['old_topic']}",
            episode_body=episode_content,
            source_description=f"Superseded by {args['actor']}",
        )
        self.ledger.append(
            actor=args["actor"],
            action="supersede",
            summary=f"Superseded {args['old_topic']}: {args['new_decision']}",
            payload={"old_topic": args["old_topic"], "reason": args["reason"]},
        )
        return {"status": "superseded", "topic": args["old_topic"]}

    async def _handle_phase(self, args: dict) -> dict:
        current_phase = _read_phase()
        if "set_current" in args or "set_next" in args:
            new_current = args.get("set_current", current_phase.get("current", ""))
            new_next = args.get("set_next", current_phase.get("next", ""))
            _write_phase(new_current, new_next)
            self.ledger.append(
                actor=args.get("actor", "system"),
                action="phase_change",
                summary=f"Phase: {new_current} -> next: {new_next}",
            )
            return {"current": new_current, "next": new_next, "status": "updated"}
        return current_phase

    async def _handle_history(self, args: dict) -> dict:
        since = None
        if "since" in args:
            since = _parse_duration(args["since"])
        entries = self.ledger.query(
            actor=args.get("actor"),
            action=args.get("action"),
            since=since,
            limit=args.get("limit", 50),
        )
        return {
            "entries": [
                {
                    "id": e.id,
                    "ts": e.ts,
                    "actor": e.actor,
                    "action": e.action,
                    "summary": e.summary,
                    "ref": e.ref,
                }
                for e in entries
            ],
            "count": len(entries),
        }


async def run_server() -> None:
    naz = NazarethServer()
    async with stdio_server() as (read_stream, write_stream):
        await naz.server.run(read_stream, write_stream, naz.server.create_initialization_options())


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
