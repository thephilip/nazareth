"""Tests for semantic search: the derived vector store and hybrid ranking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nazareth.cli import main
from nazareth.memory import vectors
from nazareth.memory.service import Memory, NazarethError
from nazareth.memory.vectors import VectorStore, rrf
from nazareth.paths import StatePaths
from tests.conftest import BrokenEmbedder, FakeEmbedder, FlakyEmbedder

ARCH = "lens:architect"


@pytest.fixture
def paths(tmp_path: Path) -> StatePaths:
    p = StatePaths(tmp_path)
    p.init()
    return p


def _seed(mem: Memory) -> dict[str, str]:
    a = mem.decide("lens-actor", "Each lens runs with a fixed NAZARETH_ACTOR",
                   "Lenses must not pose as one another", actor=ARCH)
    b = mem.decide("storage", "Keep the index in SQLite", "Simple, local", actor=ARCH)
    q = mem.ask("Should the CLI refuse writes from lenses?", actor=ARCH)
    e = mem.log("artifact", "Wrote the ledger chain", actor=ARCH, ref="chain.py")
    return {"identity": a["id"], "storage": b["id"], "question": q["id"], "event": e["id"]}


def test_lazy_embedding_covers_every_searchable_kind(paths: StatePaths) -> None:
    fake = FakeEmbedder()
    mem = Memory(paths, embedder=fake)
    ids = _seed(mem)
    assert fake.embedded == []  # nothing on the write path
    assert not paths.vectors.exists()

    mem.search("ledger")
    stored = dict(mem.vectors.db.execute("SELECT id, kind FROM vectors"))
    assert stored == {ids["identity"]: "decision", ids["storage"]: "decision",
                      ids["question"]: "question", ids["event"]: "event"}

    # A second search embeds only the query; a new record is picked up lazily.
    fake.embedded.clear()
    mem.search("ledger")
    assert fake.embedded == ["ledger"]
    new = mem.constrain("Python only", "Team skills", actor=ARCH)
    fake.embedded.clear()
    mem.search("ledger")
    assert len(fake.embedded) == 2 and "Python only" in fake.embedded[0]
    assert new["id"] in dict(mem.vectors.db.execute("SELECT id, kind FROM vectors"))
    mem.close()


def test_hybrid_finds_what_keywords_miss(paths: StatePaths) -> None:
    keyword_only = Memory(paths, embedder=None)
    ids = _seed(keyword_only)
    assert keyword_only.search("identity")["records"] == []
    keyword_only.close()

    mem = Memory(paths, embedder=FakeEmbedder())
    hits = [r["id"] for r in mem.search("identity")["records"]]
    assert hits == [ids["identity"]]
    # Keyword hits still come first and keep their shape.
    out = mem.search("sqlite")
    assert out["records"][0]["id"] == ids["storage"]
    assert set(out) == {"records", "questions", "events"}
    assert {"id", "kind", "text", "status", "actor", "ts"} <= set(out["records"][0])
    mem.close()


def test_hybrid_respects_inactive_filter(paths: StatePaths) -> None:
    mem = Memory(paths, embedder=FakeEmbedder())
    old = mem.decide("auth", "Each actor uses an API key", "MVP", actor=ARCH)
    new = mem.decide("auth", "Each actor uses OAuth", "Customers", actor=ARCH,
                     supersedes=[old["id"]])
    assert [r["id"] for r in mem.search("identity")["records"]] == [new["id"]]
    hits = {r["id"]: r for r in mem.search("identity", include_inactive=True)["records"]}
    assert hits[old["id"]]["current"] == new["id"]
    assert len(mem.search("identity", include_inactive=True, limit=1)["records"]) == 1
    mem.close()


def test_reembeds_when_text_changes(tmp_path: Path) -> None:
    store, fake = VectorStore(tmp_path / "v.db"), FakeEmbedder()
    assert store.sync([("a", "decision", "use sqlite"), ("b", "event", "x")], fake) == 2
    assert store.sync([("a", "decision", "use sqlite"), ("b", "event", "x")], fake) == 0
    assert store.sync([("a", "decision", "use postgres"), ("b", "event", "x")], fake) == 1
    assert store.nearest("postgres", fake)[0][0] == "a"
    assert store.sync([("a", "decision", "use postgres")], fake) == 0
    assert store.count() == 1  # b dropped
    store.close()


def test_embedder_change_reembeds_everything(paths: StatePaths) -> None:
    mem = Memory(paths, embedder=FakeEmbedder("fake:v1"))
    _seed(mem)
    mem.search("ledger")
    mem.close()

    v2 = FakeEmbedder("fake:v2")
    mem = Memory(paths, embedder=v2)
    mem.search("ledger")
    assert len(v2.embedded) == 5  # four documents + the query
    assert mem.vectors.embedder_name() == "fake:v2"
    names = {r[0] for r in mem.vectors.db.execute("SELECT DISTINCT embedder FROM vectors")}
    assert names == {"fake:v2"}
    mem.close()


def test_falls_back_to_keywords_when_embedder_unavailable(paths: StatePaths) -> None:
    mem = Memory(paths, embedder=BrokenEmbedder())
    ids = _seed(mem)
    assert [r["id"] for r in mem.search("sqlite")["records"]] == [ids["storage"]]
    assert "ConnectionError" in mem.vector_error
    with pytest.raises(NazarethError, match="unavailable"):
        mem.embed()
    mem.close()


def test_no_extra_means_keyword_search(paths: StatePaths) -> None:
    mem = Memory(paths)  # conftest makes default_embedder() return None
    ids = _seed(mem)
    assert [r["id"] for r in mem.search("sqlite")["records"]] == [ids["storage"]]
    assert mem.vector_error is None and not paths.vectors.exists()
    with pytest.raises(NazarethError, match=r"nazareth\[search\]"):
        mem.embed()
    mem.close()


def test_model2vec_load_failure_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    def offline(*_a, **_k):
        raise OSError("offline")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", offline)
    with pytest.raises(vectors.EmbedderUnavailable, match="can't load"):
        vectors.Model2VecEmbedder().embed(["x"])


def test_rrf_fuses_rankings() -> None:
    a, b, c = ("a", "decision"), ("b", "decision"), ("c", "event")
    assert rrf([a, b], [c, b]) == [b, a, c]
    assert rrf([a, b], []) == [a, b]


def test_rebuild_resets_vectors(paths: StatePaths) -> None:
    mem = Memory(paths, embedder=FakeEmbedder())
    _seed(mem)
    mem.search("ledger")
    assert paths.vectors.exists()
    mem.rebuild()
    assert not paths.vectors.exists()
    assert mem.search("identity")["records"]  # recomputed lazily
    mem.close()


def test_init_gitignores_vectors(tmp_path: Path) -> None:
    p = StatePaths(tmp_path)
    p.state.mkdir()
    (p.state / ".gitignore").write_text("index.db*\n*.lock")  # older layout
    p.init()
    lines = (p.state / ".gitignore").read_text().splitlines()
    assert lines == ["index.db*", "*.lock", "vectors.db*", "graph.db*"]


def test_embed_cli(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    main(["--dir", str(tmp_path), "init"])
    mem = Memory(StatePaths(tmp_path), embedder=None)
    _seed(mem)
    mem.close()
    capsys.readouterr()

    with pytest.raises(SystemExit):
        main(["--dir", str(tmp_path), "embed"])
    assert "nazareth[search]" in capsys.readouterr().err

    monkeypatch.setattr(vectors, "default_embedder", lambda: FakeEmbedder())
    main(["--dir", str(tmp_path), "embed"])
    assert capsys.readouterr().out.strip() == "4 items embedded with fake:bow-v1 (4 new)."
    main(["--dir", str(tmp_path), "query", "identity"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["records"][0]["topic"] == "lens-actor"
    assert captured.err == ""

    main(["--dir", str(tmp_path), "rebuild"])
    assert "vectors and graph reset" in capsys.readouterr().out
    assert not (tmp_path / ".nazareth" / "vectors.db").exists()


def test_query_cli_notes_fallback_once(tmp_path: Path, capsys,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    main(["--dir", str(tmp_path), "init"])
    main(["--dir", str(tmp_path), "decide", "storage", "Use SQLite", "--why", "simple"])
    capsys.readouterr()
    monkeypatch.setattr(vectors, "default_embedder", lambda: BrokenEmbedder())
    main(["--dir", str(tmp_path), "query", "sqlite"])
    captured = capsys.readouterr()
    assert len(json.loads(captured.out)["records"]) == 1
    assert captured.err.count("\n") == 1 and "keyword search only" in captured.err


def test_load_failure_disables_vectors_for_the_process(paths: StatePaths) -> None:
    broken = BrokenEmbedder()
    mem = Memory(paths, embedder=broken)
    _seed(mem)
    mem.search("sqlite")
    mem.search("sqlite")
    assert broken.calls == 1 and not mem.semantic_enabled
    assert "ConnectionError" in mem.vector_note
    mem.close()


def test_transient_failure_affects_only_that_search(paths: StatePaths) -> None:
    mem = Memory(paths, embedder=FlakyEmbedder())
    ids = _seed(mem)
    assert mem.search("identity")["records"] == []  # keyword-only this once
    assert "database is locked" in mem.vector_note
    assert mem.vector_error is None and mem.semantic_enabled
    assert [r["id"] for r in mem.search("identity")["records"]] == [ids["identity"]]
    assert mem.vector_note is None
    mem.close()


def test_creating_vectors_db_gitignores_it_in_older_projects(paths: StatePaths) -> None:
    gitignore = paths.state / ".gitignore"
    gitignore.write_text("index.db*\n*.lock\n# keep: my notes\nnotes.txt")
    mem = Memory(paths, embedder=FakeEmbedder())
    _seed(mem)
    mem.search("ledger")
    assert paths.vectors.exists()
    assert gitignore.read_text() == (
        "index.db*\n*.lock\n# keep: my notes\nnotes.txt\nvectors.db*\ngraph.db*\n")
    mem.rebuild()
    mem.search("ledger")  # recreated: nothing appended twice
    assert gitignore.read_text().count("vectors.db*") == 1
    mem.close()


def test_ensure_gitignore_never_creates_state(tmp_path: Path) -> None:
    p = StatePaths(tmp_path)
    p.ensure_gitignore()
    assert not p.state.exists()
