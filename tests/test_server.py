"""End-to-end tests through a real MCP client session."""

import json
from pathlib import Path

import anyio
import pytest
from mcp.client.client import Client

from nazareth.memory.service import Memory
from nazareth.paths import StatePaths
from nazareth.server import build_server

TOOLS = {"brief", "query", "decide", "constrain", "ask", "log", "phase", "history"}


def _run(paths: StatePaths, steps):
    async def main():
        async with Client(build_server(paths)) as client:
            return await steps(client)

    return anyio.run(main)


async def _call(client: Client, name: str, **args):
    result = await client.call_tool(name, args)
    text = result.content[0].text
    if result.is_error:
        return {"error": text}
    return json.loads(text)


@pytest.fixture(autouse=True)
def _no_fixed_actor(monkeypatch) -> None:
    # A lens running the suite may have NAZARETH_ACTOR set; tests opt in explicitly.
    monkeypatch.delenv("NAZARETH_ACTOR", raising=False)


@pytest.fixture
def paths(tmp_path: Path) -> StatePaths:
    p = StatePaths(tmp_path)
    p.init()
    return p


def test_lists_tools_with_instructions(paths: StatePaths) -> None:
    async def steps(client):
        tools = (await client.list_tools()).tools
        return {t.name for t in tools}, all(t.description for t in tools)

    names, described = _run(paths, steps)
    assert names == TOOLS
    assert described


def test_decide_supersede_query_roundtrip(paths: StatePaths) -> None:
    async def steps(client):
        a = await _call(client, "decide", topic="auth", decision="API keys", rationale="MVP",
                        actor="lens:architect")
        b = await _call(client, "decide", topic="auth", decision="OAuth", rationale="customers",
                        supersedes=[a["id"]], actor="lens:architect")
        stale = await _call(client, "decide", topic="auth", decision="SAML", rationale="old ctx",
                            supersedes=[a["id"]], actor="lens:code")
        read = await _call(client, "query", id=a["id"])
        brief = await _call(client, "brief")
        return a, b, stale, read, brief

    a, b, stale, read, brief = _run(paths, steps)
    assert b["status"] == "active"
    assert b["id"] in stale["error"]  # the agent is told what's current, not a generic failure
    assert read["id"] == a["id"]
    assert read["current"] == b["id"]
    assert [d["id"] for d in brief["decisions"]] == [b["id"]]


def test_actor_defaults_from_env(paths: StatePaths, monkeypatch) -> None:
    monkeypatch.setenv("NAZARETH_ACTOR", "lens:reviewer")

    async def steps(client):
        out = await _call(client, "log", action="finding", summary="unsafe compare",
                          severity="high")
        return await _call(client, "query", id=out["id"])

    assert _run(paths, steps)["actor"] == "lens:reviewer"


def test_errors_reach_the_agent(paths: StatePaths) -> None:
    async def steps(client):
        return (
            await _call(client, "query"),
            await _call(client, "log", action="decision", summary="x"),
            await _call(client, "history", since="whenever"),
        )

    no_args, reserved, bad_since = _run(paths, steps)
    assert "Pass `text`" in no_args["error"]
    assert "reserved" in reserved["error"]
    assert "Can't parse" in bad_since["error"]


def test_uninitialized_project_explains_itself(tmp_path: Path) -> None:
    paths = StatePaths(tmp_path)

    async def steps(client):
        return await _call(client, "brief")

    out = _run(paths, steps)
    assert "nazareth init" in out["error"]
    assert not paths.state.exists()  # serving must never create state on its own


def test_phase_read_and_set(paths: StatePaths) -> None:
    async def steps(client):
        before = await _call(client, "phase")
        after = await _call(client, "phase", set_current="mvp", actor="lens:architect")
        return before, after

    before, after = _run(paths, steps)
    assert before["current"] == "brainstorm"
    assert after == {"current": "mvp", "next": "scaffold"}


def test_env_actor_beats_argument(paths: StatePaths, monkeypatch) -> None:
    monkeypatch.setenv("NAZARETH_ACTOR", "lens:code")

    async def steps(client):
        out = await _call(client, "log", action="artifact", summary="x",
                          actor="lens:architect")
        return await _call(client, "query", id=out["id"])

    assert _run(paths, steps)["actor"] == "lens:code"


def test_argument_used_without_env(paths: StatePaths) -> None:
    async def steps(client):
        out = await _call(client, "log", action="artifact", summary="x",
                          actor=" lens:architect ")
        return await _call(client, "query", id=out["id"])

    assert _run(paths, steps)["actor"] == "lens:architect"


@pytest.mark.parametrize("claimed", ["human", " Human ", "HUMAN"])
def test_human_argument_rejected(paths: StatePaths, claimed: str) -> None:
    async def steps(client):
        return (
            await _call(client, "log", action="artifact", summary="x", actor=claimed),
            await _call(client, "history"),
        )

    out, history = _run(paths, steps)
    assert "nazareth" in out["error"] and "CLI" in out["error"]
    assert not [e for e in history["entries"] if e["action"] == "artifact"]


def test_human_env_rejected(paths: StatePaths, monkeypatch) -> None:
    monkeypatch.setenv("NAZARETH_ACTOR", " Human")

    async def steps(client):
        return await _call(client, "decide", topic="t", decision="d", rationale="r")

    assert "CLI" in _run(paths, steps)["error"]


def test_lens_cannot_supersede_human_by_claiming_human(paths: StatePaths, monkeypatch) -> None:
    human = Memory(paths).decide("storage", "SQLite", "simple", "human")
    monkeypatch.setenv("NAZARETH_ACTOR", "lens:code")

    async def steps(client):
        forged = await _call(client, "decide", topic="storage", decision="Postgres",
                             rationale="scale", supersedes=[human["id"]], actor="human")
        brief = await _call(client, "brief")
        return forged, brief

    forged, brief = _run(paths, steps)
    assert "CLI" in forged["error"]
    assert [d["id"] for d in brief["decisions"]] == [human["id"]]
