"""Tests for knowledge graph enrichment: episodes, `enrich`, and graph facts in search."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from nazareth.cli import main
from nazareth.ledger.chain import Ledger
from nazareth.memory import graph as graphs
from nazareth.memory.graph import Episode, Fact, episode, parse_name
from nazareth.memory.service import HUMAN, Memory, NazarethError
from nazareth.paths import StatePaths
from tests.conftest import FakeEmbedder

ARCH = "lens:architect"


class FakeGraph:
    """In-memory stand-in for Graphiti. State lives in `store`, so it survives
    reopening like the real file does; search matches whole words."""

    def __init__(self, store: dict, path: Path, write: bool) -> None:
        self.store, self.path, self.write = store, path, write
        store.setdefault("episodes", [])
        store.setdefault("opens", []).append(write)

    def watermark(self):
        return self.store.get("mark")

    def add(self, ep: Episode) -> int:
        assert self.write, "only enrich may add episodes"
        if self.store.get("fail_on") == ep.event_id:
            raise RuntimeError("LLM returned garbage")
        self.store["episodes"].append(ep)
        self.path.touch()
        return 0 if ep.event_id in self.store.get("empty", ()) else 2

    def set_watermark(self, seq: int, entry_hash: str) -> None:
        self.store["mark"] = (seq, entry_hash)

    def search(self, text: str, limit: int) -> list[Fact]:
        words = set(re.findall(r"\w{4,}", text.lower()))
        hits = [ep for ep in self.store["episodes"]
                if words & set(re.findall(r"\w{4,}", ep.body.lower()))]
        return [Fact(f"fact from {ep.event_id}", (ep.target,)) for ep in hits[:limit]]

    def reset(self) -> None:
        self.store.clear()

    def close(self) -> None:
        self.store["closed"] = self.store.get("closed", 0) + 1


@pytest.fixture
def paths(tmp_path: Path) -> StatePaths:
    p = StatePaths(tmp_path)
    p.init()
    return p


@pytest.fixture
def store() -> dict:
    return {}


@pytest.fixture
def mem(paths: StatePaths, store: dict) -> Memory:
    return Memory(paths, graph=lambda write: FakeGraph(store, paths.graph, write))


def _seed(mem: Memory) -> dict[str, str]:
    a = mem.decide("storage", "Keep the index in SQLite", "Simple and local", actor=ARCH)
    q = mem.ask("Should backups go to object storage?", actor=ARCH, context="Laptop only now")
    mem.answer(q["id"], "Not yet")
    mem.set_phase(HUMAN, current="mvp")
    e = mem.log("artifact", "Wrote the projection", actor="lens:code", ref="projection.py")
    return {"decision": a["id"], "question": q["id"], "event": e["id"]}


def test_episodes_point_back_to_what_they_are_about(mem: Memory) -> None:
    ids = _seed(mem)
    eps = [episode(e, lambda rid: None) for e in mem.ledger.entries()]
    targets = [ep.target if ep else None for ep in eps]
    assert targets == [(ids["decision"], "decision"), (ids["question"], "question"),
                       (ids["question"], "question"), None, (ids["event"], "event")]
    assert "Rationale: Simple and local" in eps[0].body
    assert "Context: Laptop only now" in eps[1].body
    assert parse_name(eps[0].name) == (ids["decision"], "decision")
    assert parse_name("someone else's episode") is None


def test_resolutions_are_about_the_record(paths: StatePaths, mem: Memory) -> None:
    a = mem.decide("storage", "SQLite", "simple", actor=ARCH)
    b = mem.decide("storage", "Postgres", "scale", actor="lens:code")  # pending
    mem.resolve(b["id"], accept=True, note="we need replicas")
    last = mem.ledger.entries()[-1]
    ep = episode(last, lambda rid: mem.index.record(rid)["kind"])
    assert ep.target == (b["id"], "decision")
    assert a["id"] in ep.body and "we need replicas" in ep.body


def test_enrich_sends_everything_once_in_order(mem: Memory, store: dict) -> None:
    _seed(mem)
    out = mem.enrich()
    assert out == {"sent": 4, "remaining": 0, "no_facts": []}  # phase has nothing to learn
    assert [ep.event_id for ep in store["episodes"]] == \
        [e.id for e in mem.ledger.entries() if e.action != "phase"]
    assert store["mark"][0] == mem.ledger.entries()[-1].seq
    assert mem.enrich() == {"sent": 0, "remaining": 0, "no_facts": []}
    assert store["opens"] == [True, True] and store["closed"] == 2


def test_enrich_limit_resumes_where_it_stopped(mem: Memory, store: dict) -> None:
    _seed(mem)
    steps = []
    assert mem.enrich(limit=1)["remaining"] == 4
    out = mem.enrich(limit=2, progress=steps.append)
    assert (out["sent"], out["remaining"]) == (2, 1)  # and the phase change
    assert [(s.n, s.target, s.facts) for s in steps] == [(1, 2, 2), (2, 2, 2)]
    assert mem.enrich()["sent"] == 1
    assert len({ep.event_id for ep in store["episodes"]}) == 4


def test_enrich_names_events_that_produced_no_facts(mem: Memory, store: dict) -> None:
    ids = _seed(mem)
    q_event = mem.ledger.entries()[1].id
    store["empty"] = {q_event}
    assert mem.enrich()["no_facts"] == [ids["question"]]


def test_failed_episode_keeps_progress(mem: Memory, store: dict) -> None:
    _seed(mem)
    store["fail_on"] = mem.ledger.entries()[2].id
    with pytest.raises(RuntimeError):
        mem.enrich()
    assert len(store["episodes"]) == 2
    del store["fail_on"]
    assert mem.enrich()["sent"] == 2


def test_enrich_refuses_a_graph_from_another_history(mem: Memory, store: dict) -> None:
    _seed(mem)
    mem.enrich()
    store["mark"] = (store["mark"][0], "not-this-ledger")
    with pytest.raises(NazarethError, match="nazareth rebuild"):
        mem.enrich()


def test_enrich_explains_missing_setup(paths: StatePaths, monkeypatch) -> None:
    def no_llm(write: bool):
        raise graphs.GraphUnavailable("set NAZARETH_LLM_MODEL to ...")

    def no_extra(write: bool):
        raise graphs.GraphUnavailable("graph extra not installed", missing_extra=True)

    # Missing LLM settings aren't fixed by reinstalling, so don't suggest it.
    with pytest.raises(NazarethError, match="NAZARETH_LLM_MODEL.*LLM settings") as exc:
        Memory(paths, graph=no_llm).enrich()
    assert "install" not in str(exc.value)
    with pytest.raises(NazarethError, match="install.sh.*--extras search,graph") as exc:
        Memory(paths, graph=no_extra).enrich()
    assert "pip install" not in str(exc.value)


def test_llm_settings_name_whats_missing(monkeypatch) -> None:
    for key in graphs.LLM_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NAZARETH_LLM_MODEL", "glm")
    with pytest.raises(graphs.GraphUnavailable, match="NAZARETH_LLM_BASE_URL, NAZARETH_LLM_API_KEY"):
        graphs.llm_settings()
    monkeypatch.setenv("NAZARETH_LLM_BASE_URL", "http://x/v1")
    monkeypatch.setenv("NAZARETH_LLM_API_KEY", "k")
    assert graphs.llm_settings()["NAZARETH_LLM_SMALL_MODEL"] == "glm"


def test_query_folds_in_graph_facts(paths: StatePaths, store: dict) -> None:
    writer = Memory(paths, graph=lambda write: FakeGraph(store, paths.graph, write))
    ids = _seed(writer)
    reader = Memory(paths, graph=lambda write: FakeGraph(store, paths.graph, write))
    assert "facts" not in reader.search("backups")  # nothing enriched yet: no graph opened
    assert store.get("opens", []) == []
    writer.enrich()
    out = reader.search("backups object")
    assert out["facts"][0]["sources"] == [ids["question"]]
    assert [q["id"] for q in out["questions"]] == [ids["question"]]
    assert store["opens"] == [True, False]  # readers never open the graph to write


def test_graph_only_hit_is_found(paths: StatePaths, store: dict) -> None:
    mem = Memory(paths, graph=lambda write: FakeGraph(store, paths.graph, write))
    ids = _seed(mem)
    mem.enrich()
    # "replication" appears nowhere in FTS; pretend the graph inferred it.
    store["episodes"][0] = Episode(store["episodes"][0].event_id, 0.0,
                                   (ids["decision"], "decision"), "replication strategy")
    out = mem.search("replication")
    assert [r["id"] for r in out["records"]] == [ids["decision"]]


def test_broken_graph_never_fails_a_search(paths: StatePaths) -> None:
    paths.graph.touch()

    def unavailable(write: bool):
        raise graphs.GraphUnavailable("graph extra not installed")

    mem = Memory(paths, graph=unavailable)
    mem.decide("storage", "SQLite", "simple", actor=ARCH)
    assert mem.search("SQLite")["records"]
    assert mem.graph_error == "graph extra not installed"


def test_rebuild_resets_the_graph(paths: StatePaths, mem: Memory, store: dict) -> None:
    _seed(mem)
    mem.enrich()
    (paths.state / "graph.db.settings").write_text("{}")
    mem.rebuild()
    assert not list(paths.state.glob("graph.db*"))
    assert "mark" not in store
    assert mem.enrich()["sent"] == 4


def test_enrich_cli(paths: StatePaths, store: dict, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Memory, "_default_graph",
                        lambda self, write: FakeGraph(store, paths.graph, write))
    mem = Memory(paths)
    _seed(mem)
    main(["--dir", str(paths.project), "enrich", "--limit", "2"])
    assert "Sent 2 ledger events" in capsys.readouterr().out
    main(["--dir", str(paths.project), "enrich"])
    out = capsys.readouterr().out
    assert "Sent 2 ledger events" in out and "left" not in out


def test_cli_closes_the_graph(paths: StatePaths, store: dict, monkeypatch, capsys) -> None:
    monkeypatch.setattr(Memory, "_default_graph",
                        lambda self, write: FakeGraph(store, paths.graph, write))
    _seed(Memory(paths))
    main(["--dir", str(paths.project), "enrich"])
    main(["--dir", str(paths.project), "query", "backups object"])
    assert "facts" in capsys.readouterr().out
    assert store["opens"] == [True, False] and store["closed"] == 2


def test_server_closes_the_graph(paths: StatePaths, store: dict, monkeypatch) -> None:
    import anyio
    from mcp.client.client import Client

    from nazareth.server import build_server

    monkeypatch.setattr(Memory, "_default_graph",
                        lambda self, write: FakeGraph(store, paths.graph, write))
    mem = Memory(paths)
    _seed(mem)
    mem.enrich()
    server = build_server(paths)

    async def go():
        async with Client(server) as client:
            await client.call_tool("query", {"text": "backups object"})

    anyio.run(go)
    server.close_memory()
    assert store["opens"] == [True, False] and store["closed"] == 2


def test_graph_db_is_gitignored(paths: StatePaths) -> None:
    assert "graph.db*" in (paths.state / ".gitignore").read_text().splitlines()


# -- the real Graphiti, offline ------------------------------------------------

needs_graph = pytest.mark.skipif(not graphs.graph_extra_installed(), reason="graph extra")


@needs_graph
def test_graphiti_store_persists_watermark_and_searches_offline(tmp_path: Path) -> None:
    """Opens embedded FalkorDB with no LLM reachable: building indices,
    searching and the watermark must not need one, and must survive reopening."""
    path = tmp_path / "graph.db"
    g = graphs.GraphitiGraph(path, FakeEmbedder())
    assert g.watermark() is None
    assert g.search("anything", 5) == []
    g.set_watermark(7, "abc")
    g.close()
    assert path.exists()
    g = graphs.GraphitiGraph(path, FakeEmbedder())
    assert g.watermark() == (7, "abc")
    g.reset()
    assert g.watermark() is None
    g.close()


@needs_graph
def test_incomplete_extra_is_unavailable_not_a_crash(tmp_path: Path, monkeypatch) -> None:
    async def broken(self, embedder, llm):
        raise ModuleNotFoundError("No module named 'httpx'")

    monkeypatch.setattr(graphs.GraphitiGraph, "_open", broken)
    with pytest.raises(graphs.GraphUnavailable, match="incomplete.*httpx") as exc:
        graphs.GraphitiGraph(tmp_path / "graph.db", FakeEmbedder())
    assert exc.value.missing_extra


@needs_graph
@pytest.mark.skipif(not all(os.environ.get(k) for k in graphs.LLM_ENV),
                    reason="needs NAZARETH_LLM_* for a real extraction LLM")
def test_graphiti_end_to_end(paths: StatePaths, monkeypatch) -> None:
    monkeypatch.undo()  # use the real default graph
    mem = Memory(paths, embedder=FakeEmbedder())
    a = mem.decide("storage", "Keep the index in SQLite", "Simple and local", actor=ARCH)
    assert mem.enrich()["sent"] == 1
    facts = mem.facts("SQLite index")
    assert facts and all(src == (a["id"], "decision") for f in facts for src in f.sources)
    Ledger(paths.ledger)  # the ledger is untouched by enrichment
    mem.close()
