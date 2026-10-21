"""Tests for the memory service's write policy, reads, and rebuildability."""

from pathlib import Path

import pytest

from nazareth.memory.service import HUMAN, Memory, NazarethError, parse_since
from nazareth.paths import StatePaths

ARCH = "lens:architect"
CODE = "lens:code"


@pytest.fixture
def paths(tmp_path: Path) -> StatePaths:
    p = StatePaths(tmp_path)
    p.init()
    return p


@pytest.fixture
def mem(paths: StatePaths) -> Memory:
    m = Memory(paths)
    yield m
    m.close()


def test_requires_init(tmp_path: Path) -> None:
    with pytest.raises(NazarethError, match="nazareth init"):
        Memory(StatePaths(tmp_path))


def test_init_ignores_derived_files(paths: StatePaths) -> None:
    assert "index.db" in (paths.state / ".gitignore").read_text()


def test_first_decision_is_active(mem: Memory) -> None:
    out = mem.decide("Auth", "Use API keys", "Simplest for MVP", actor=ARCH)
    assert out["status"] == "active"
    rec = mem.get(out["id"])
    assert rec["topic"] == "auth"
    assert rec["rationale"] == "Simplest for MVP"
    assert rec["actor"] == ARCH


def test_same_topic_without_supersede_is_pending(mem: Memory) -> None:
    first = mem.decide("auth", "Use API keys", "MVP", actor=ARCH)
    second = mem.decide("auth", "Use OAuth", "Enterprise customers", actor=CODE)
    assert second["status"] == "pending"
    assert first["id"] in second["pending_reason"]
    assert mem.get(first["id"])["status"] == "active"


def test_explicit_supersede_replaces(mem: Memory) -> None:
    first = mem.decide("auth", "Use API keys", "MVP", actor=ARCH)
    second = mem.decide("auth", "Use OAuth", "Customers asked", actor=ARCH,
                        supersedes=[first["id"]])
    assert second["status"] == "active"
    old = mem.get(first["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"] == second["id"]
    assert mem.get(second["id"])["supersedes"] == [first["id"]]


def test_superseding_stale_record_points_to_current(mem: Memory) -> None:
    a = mem.decide("db", "SQLite", "zero ops", actor=ARCH)
    b = mem.decide("db", "Postgres", "concurrency", actor=ARCH, supersedes=[a["id"]])
    c = mem.decide("db", "DuckDB", "analytics", actor=ARCH, supersedes=[b["id"]])
    with pytest.raises(NazarethError) as exc:
        mem.decide("db", "MySQL", "stale agent", actor=CODE, supersedes=[a["id"]])
    assert c["id"] in str(exc.value)
    assert mem.get(a["id"])["current"] == c["id"]


def test_lens_cannot_override_human(mem: Memory) -> None:
    h = mem.decide("scope", "No OAuth for MVP", "keep it small", actor=HUMAN)
    out = mem.decide("scope", "Add OAuth", "seems useful", actor=CODE, supersedes=[h["id"]])
    assert out["status"] == "pending"
    assert "human-made" in out["pending_reason"]
    assert mem.get(h["id"])["status"] == "active"


def test_human_can_override_human(mem: Memory) -> None:
    h = mem.decide("scope", "No OAuth for MVP", "keep it small", actor=HUMAN)
    out = mem.decide("scope", "Add OAuth", "customer deal", actor=HUMAN, supersedes=[h["id"]])
    assert out["status"] == "active"


def test_resolve_accept_supersedes_conflicts(mem: Memory) -> None:
    a = mem.decide("auth", "API keys", "MVP", actor=ARCH)
    p = mem.decide("auth", "OAuth", "customers", actor=CODE)
    out = mem.resolve(p["id"], accept=True, note="customers win")
    assert out["superseded"] == [a["id"]]
    assert mem.get(p["id"])["status"] == "active"
    assert mem.get(a["id"])["status"] == "superseded"


def test_resolve_reject(mem: Memory) -> None:
    mem.decide("auth", "API keys", "MVP", actor=ARCH)
    p = mem.decide("auth", "OAuth", "customers", actor=CODE)
    mem.resolve(p["id"], accept=False, note="not now")
    rec = mem.get(p["id"])
    assert rec["status"] == "rejected"
    assert rec["status_reason"] == "not now"


def test_resolve_requires_pending(mem: Memory) -> None:
    a = mem.decide("auth", "API keys", "MVP", actor=ARCH)
    with pytest.raises(NazarethError, match="not pending"):
        mem.resolve(a["id"], accept=True)


def test_kinds_do_not_cross(mem: Memory) -> None:
    c = mem.constrain("Python only", "team skills", actor=HUMAN)
    with pytest.raises(NazarethError, match="constraint"):
        mem.decide("lang", "Go", "speed", actor=ARCH, supersedes=[c["id"]])


def test_unknown_ids_rejected(mem: Memory) -> None:
    with pytest.raises(NazarethError, match="Unknown record"):
        mem.decide("x", "y", "z", actor=ARCH, supersedes=["dec-nope"])
    with pytest.raises(NazarethError, match="Unknown record"):
        mem.decide("x", "y", "z", actor=ARCH, depends_on=["dec-nope"])


def test_depends_on_stale_warns(mem: Memory) -> None:
    a = mem.decide("db", "SQLite", "zero ops", actor=ARCH)
    b = mem.decide("db", "Postgres", "concurrency", actor=ARCH, supersedes=[a["id"]])
    out = mem.decide("orm", "SQLAlchemy", "works with it", actor=CODE, depends_on=[a["id"]])
    assert out["status"] == "active"
    assert b["id"] in out["warnings"][0]
    assert mem.get(a["id"])["dependents"] == [out["id"]]


def test_related_active_surfaces_overlap(mem: Memory) -> None:
    a = mem.decide("authentication", "Use API keys for authentication", "MVP", actor=ARCH)
    out = mem.decide("auth-tokens", "Rotate auth tokens daily", "security", actor=ARCH)
    assert out["status"] == "active"
    assert a["id"] in [r["id"] for r in out["related_active"]]


def test_constraints_and_retire(mem: Memory) -> None:
    c = mem.constrain("No OAuth for MVP", "scope", actor=HUMAN)
    assert [r["id"] for r in mem.brief()["constraints"]] == [c["id"]]
    mem.retire(c["id"], reason="MVP shipped")
    assert mem.brief()["constraints"] == []
    assert mem.get(c["id"])["status"] == "retired"


def test_questions_flow(mem: Memory) -> None:
    q = mem.ask("Should we support Windows?", actor=CODE, context="CI matrix", ref="ci.yml")
    assert mem.brief()["open_questions"][0]["id"] == q["id"]
    mem.answer(q["id"], "No, macOS and Linux only")
    brief = mem.brief()
    assert "open_questions" not in brief
    assert brief["recent_answers"][0]["answer"] == "No, macOS and Linux only"
    with pytest.raises(NazarethError, match="already answered"):
        mem.answer(q["id"], "again")


def test_phase(mem: Memory) -> None:
    assert mem.phase() == {"current": "brainstorm", "next": "scaffold"}
    mem.set_phase(HUMAN, current="mvp")
    assert mem.phase() == {"current": "mvp", "next": "scaffold"}


def test_log_rejects_reserved_actions(mem: Memory) -> None:
    with pytest.raises(NazarethError, match="reserved"):
        mem.log("decision", "sneaky", actor=CODE)
    out = mem.log("Finding", "Key compare not constant-time", actor="lens:reviewer",
                  ref="src/auth.py:23", severity="High")
    ev = mem.get(out["id"])
    assert ev["action"] == "finding"
    assert ev["severity"] == "high"


def test_search_hides_superseded_unless_asked(mem: Memory) -> None:
    a = mem.decide("db", "Use SQLite storage", "zero ops", actor=ARCH)
    b = mem.decide("db", "Use Postgres storage", "concurrency", actor=ARCH,
                   supersedes=[a["id"]])
    ids = [r["id"] for r in mem.search("storage")["records"]]
    assert ids == [b["id"]]
    hits = {r["id"]: r for r in mem.search("storage", include_inactive=True)["records"]}
    assert hits[a["id"]]["current"] == b["id"]


def test_search_covers_events_and_questions(mem: Memory) -> None:
    mem.log("artifact", "Scaffolded billing module", actor=CODE, ref="src/billing.py")
    mem.ask("Which billing provider?", actor=CODE)
    out = mem.search("billing")
    assert len(out["events"]) == 1
    assert len(out["questions"]) == 1


def test_search_survives_punctuation(mem: Memory) -> None:
    mem.decide("api", "REST over gRPC", "tooling", actor=ARCH)
    assert mem.search('what about "gRPC" (AND) -- OR *?')["records"]
    assert mem.search("the and of")["records"] == []


def test_history_filters(mem: Memory) -> None:
    mem.log("artifact", "one", actor=CODE)
    mem.decide("x", "y", "z", actor=ARCH)
    assert mem.history(actor=CODE)["count"] == 1
    assert mem.history(action="decision")["count"] == 1
    assert mem.history(since="1h")["count"] == 2
    with pytest.raises(NazarethError, match="Can't parse"):
        mem.history(since="yesterday-ish")


def test_parse_since_iso() -> None:
    assert parse_since("2026-01-01") < parse_since("1d")


def test_rebuild_reproduces_state(mem: Memory) -> None:
    a = mem.decide("auth", "API keys", "MVP", actor=ARCH)
    p = mem.decide("auth", "OAuth", "customers", actor=CODE)
    mem.resolve(p["id"], accept=True)
    mem.constrain("Python only", "skills", actor=HUMAN)
    q = mem.ask("Windows?", actor=CODE)
    mem.answer(q["id"], "no")
    mem.set_phase(HUMAN, current="mvp", next_phase="harden")
    mem.log("artifact", "auth module", actor=CODE)
    before = mem.brief()
    before_a = mem.get(a["id"])

    mem.index.rebuild()
    after = mem.brief()
    assert after == before
    assert mem.get(a["id"]) == before_a


def test_index_deleted_is_rebuilt(paths: StatePaths) -> None:
    m = Memory(paths)
    a = m.decide("auth", "API keys", "MVP", actor=ARCH)
    m.close()
    for f in paths.state.glob("index.db*"):
        f.unlink()
    m2 = Memory(paths)
    assert m2.get(a["id"])["status"] == "active"
    m2.close()


def test_two_instances_see_each_other(paths: StatePaths) -> None:
    m1, m2 = Memory(paths), Memory(paths)
    a = m1.decide("auth", "API keys", "MVP", actor=ARCH)
    # m2 has never read this; its write must still see the conflict.
    out = m2.decide("auth", "OAuth", "customers", actor=CODE)
    assert out["status"] == "pending"
    assert a["id"] in out["pending_reason"]
    m1.close()
    m2.close()


def test_ledger_replaced_triggers_reindex(paths: StatePaths) -> None:
    m = Memory(paths)
    m.decide("auth", "API keys", "MVP", actor=ARCH)
    m.close()
    paths.ledger.unlink()
    m2 = Memory(paths)
    b = m2.decide("db", "SQLite", "simple", actor=ARCH)
    assert [r["id"] for r in m2.brief()["decisions"]] == [b["id"]]
    m2.close()
