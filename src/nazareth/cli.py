"""Nazareth CLI: the human's side of the shared memory.

Commands run as actor "human". Lenses use the MCP server (`nazareth serve`).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from nazareth.ledger.chain import Ledger
from nazareth.memory.service import HUMAN, Memory, NazarethError
from nazareth.paths import StatePaths, find_project_dir
from nazareth.term import Term


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    fmt = "%H:%M" if dt.date() == datetime.now().date() else "%b %d %H:%M"
    return dt.strftime(fmt)


def _paths(args: argparse.Namespace) -> StatePaths:
    return StatePaths(Path(args.dir).resolve() if args.dir else find_project_dir())


_opened: list[Memory] = []  # closed when the command ends, so the graph server stops


def _memory(args: argparse.Namespace) -> Memory:
    mem = Memory(_paths(args))
    _opened.append(mem)
    return mem


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _record_line(t: Term, r: dict[str, Any]) -> str:
    """One line per record: id, [topic] and text clipped to the terminal."""
    topic = f"[{r['topic']}] " if r.get("topic") else ""
    return f"  {t.id(r['id'])}  {t.clip(topic + r['text'], t.width - 14)}"


def _print_write(t: Term, out: dict[str, Any]) -> None:
    """What a decide/constrain/resolve did, for the human."""
    print(f"{t.id(out['id'])}  {t.status(out['status'])}")
    if out.get("pending_reason"):
        print(t.warn(f"  why pending: {out['pending_reason']}"))
    if out.get("superseded"):
        print(f"  supersedes {', '.join(t.id(i) for i in out['superseded'])}")
    for w in out.get("warnings", []) + out.get("skipped", []):
        print(t.warn(f"  {w}"))
    if out.get("related_active"):
        print(t.meta("  related, check for overlap:"))
        for r in out["related_active"]:
            print("  " + _record_line(t, r))


def _event_line(t: Term, e: dict[str, Any], indent: int = 0) -> str:
    """time, actor, action and summary on one line; ref appended when it fits."""
    ref = f" ({e['ref']})" if e.get("ref") else ""
    room = t.width - indent - 42
    return (" " * indent + t.meta(f"{_when(e['ts']):>12}") + "  " + t.actor(e["actor"], 16)
            + " " + t.heading(f"{e['action']:<10}") + " " + t.clip(e["summary"] + ref, room))


# -- commands -----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    from nazareth import claude_code, docker_agent

    if args.model and not args.docker:
        raise NazarethError("--model sets the Docker Agent lens model; use it with --docker.")
    paths = _paths(args)
    # Everything that can fail is checked first: the Claude Code config and the
    # Docker model, so a bad one stops init before anything (including
    # .nazareth/) is created.
    if args.claude:
        claude_code.install(paths.project, check_only=True)
    if args.docker:
        docker_agent.render(args.model)
    report = claude_code.install(paths.project) if args.claude else []
    if args.docker:
        report += docker_agent.install(paths.project, args.model)
    existed = paths.initialized
    paths.init()
    Memory(paths).close()
    print(f"{'Already initialized' if existed else 'Initialized'}: {paths.state}")
    for path, what in report:
        print(f"  {path}: {what}")
    if args.docker:
        config = f"{docker_agent.CONFIG_DIR}/{docker_agent.CONFIG}"
        print(f"Run the lenses with `{docker_agent.command()} run {config}` "
              "(or add `-a lens-code`).")
        if not docker_agent.found():
            print("Docker Agent isn't installed; install it with `brew install docker-agent`.")
    if (args.claude or args.docker) and (warning := claude_code.path_warning(sys.argv[0])):
        print(f"Warning: {warning}", file=sys.stderr)


def cmd_context(args: argparse.Namespace) -> None:
    """For Claude Code's SessionStart hook: print a memory snapshot, or
    nothing at all if this project doesn't use nazareth."""
    from nazareth.claude_code import render_context

    paths = _paths(args)
    if not paths.initialized:
        return
    try:
        mem = Memory(paths)
        print(render_context(mem.brief()))
    except Exception as e:  # noqa: BLE001 - a traceback at every session start helps nobody
        print(f"## Project memory (nazareth)\nUnavailable: {type(e).__name__}: {e}\n"
              "Ask the human to run `nazareth verify`; don't assume earlier decisions.")


def cmd_serve(_args: argparse.Namespace) -> None:
    from nazareth.server import run_server

    run_server()


def cmd_verify(args: argparse.Namespace) -> None:
    paths = _paths(args)
    if not paths.initialized:
        raise NazarethError(f"Nazareth isn't initialized for {paths.project}; run `nazareth init`.")
    valid, message = Ledger(paths.ledger).verify()
    print(message)
    if not valid:
        sys.exit(1)


def cmd_rebuild(args: argparse.Namespace) -> None:
    mem = _memory(args)
    mem.rebuild()
    print(f"Rebuilt index from {len(mem.ledger)} ledger entries; vectors and graph reset.")


def cmd_embed(args: argparse.Namespace) -> None:
    out = _memory(args).embed()
    print(f"{out['count']} items embedded with {out['embedder']} ({out['embedded']} new).")


def cmd_enrich(args: argparse.Namespace) -> None:
    from nazareth.memory.service import EnrichStep

    t = Term()

    def progress(step: EnrichStep) -> None:
        facts = (t.warn("no facts") if not step.facts
                 else f"{step.facts} fact{'s' if step.facts != 1 else ''}")
        n = f"{step.n}/{step.target}".rjust(2 * len(str(step.target)) + 1)
        print(f"  {n}  {step.kind:<10} {t.id(step.id.ljust(20))} "
              f"{t.meta(f'{step.seconds:4.0f}s')}  {facts}", flush=True)

    print(t.meta("Sending ledger events to the knowledge graph; each takes several LLM calls."),
          flush=True)
    out = _memory(args).enrich(limit=args.limit, progress=progress)
    more = f"; {out['remaining']} left, run again to continue" if out["remaining"] else ""
    print(f"Sent {out['sent']} ledger events to the knowledge graph{more}.")
    if out["no_facts"]:
        ids = ", ".join(out["no_facts"])
        print(t.warn(f"No facts came back for {ids}. The model may have failed to extract "
                     "them; they won't be retried unless you `nazareth rebuild` and enrich "
                     "again, so consider a stronger model."))


def cmd_status(args: argparse.Namespace) -> None:
    mem = _memory(args)
    b = mem.brief()
    if args.json:
        _print_json(b)
        return
    t = Term()
    valid, chain = mem.ledger.verify()
    ph = b["phase"]
    print(f"{t.heading('nazareth')} {t.meta('·')} {mem.paths.project}")
    print(f"Phase: {t.heading(ph['current'])} {t.meta('→ next:')} {ph['next']}")
    print(f"Ledger: {chain}" + ("" if valid else "  " + t.alert("⚠ run `nazareth verify`")))

    pending, open_q = b.get("pending_review", []), b.get("open_questions", [])
    print()
    if pending or open_q:
        print(t.alert(f"NEEDS YOU ({len(pending) + len(open_q)})"))
        for r in pending:
            print(_record_line(t, r) + "   " + t.warn("(pending)"))
            print(t.meta(f"      why pending: {t.clip(r['reason'], t.width - 19)}"))
        for q in open_q:
            print(f"  {t.id(q['id'])}  {t.clip(q['question'], t.width - 12)}")
            meta = f"asked by {q['asked_by']} {_when(q['ts'])}"
            if q.get("context"):
                meta += f" · {q['context']}"
            print(t.meta(f"      {t.clip(meta, t.width - 6)}"))
        print(t.meta('  → nazareth answer <q-id> "..."   ·   '
                     "nazareth resolve <id> --accept|--reject"))
    else:
        print(f"NEEDS YOU: {t.ok('nothing')}")

    for title, rows in (("CONSTRAINTS", b["constraints"]), ("DECISIONS", b["decisions"])):
        print(f"\n{t.heading(title)} {t.meta(f'({len(rows)})')}")
        for r in rows:
            print(_record_line(t, r))
    if b.get("decisions_truncated"):
        print(t.meta(f"  … {b['decisions_truncated']}"))

    if b.get("recent_answers"):
        print("\n" + t.heading("RECENT ANSWERS"))
        for q in b["recent_answers"][:5]:
            print(f"  {t.id(q['id'])}  "
                  f"{t.clip(q['question'] + ' → ' + q['answer'], t.width - 12)}")

    print("\n" + t.heading("RECENT ACTIVITY"))
    for e in reversed(b["recent_activity"]):
        print(_event_line(t, e, indent=2))


def cmd_decide(args: argparse.Namespace) -> None:
    out = _memory(args).decide(args.topic, args.decision, args.why, actor=HUMAN,
                               supersedes=args.supersedes, depends_on=args.depends_on)
    _print_json(out) if args.json else _print_write(Term(), out)


def cmd_constrain(args: argparse.Namespace) -> None:
    out = _memory(args).constrain(args.constraint, args.why, actor=HUMAN, topic=args.topic,
                                  supersedes=args.supersedes)
    _print_json(out) if args.json else _print_write(Term(), out)


def cmd_answer(args: argparse.Namespace) -> None:
    out = _memory(args).answer(args.question_id, args.answer)
    print(f"{out['id']}: answered")


def cmd_resolve(args: argparse.Namespace) -> None:
    out = _memory(args).resolve(args.record_id, accept=args.accept, note=args.note)
    _print_json(out) if args.json else _print_write(Term(), out)


def cmd_retire(args: argparse.Namespace) -> None:
    out = _memory(args).retire(args.record_id, args.why)
    print(f"{out['id']}: retired")


def cmd_phase(args: argparse.Namespace) -> None:
    mem = _memory(args)
    current = args.current or args.phase
    if current or args.next:
        ph = mem.set_phase(HUMAN, current=current, next_phase=args.next)
    else:
        ph = mem.phase()
    print(f"{ph['current']} → next: {ph['next']}")


def cmd_query(args: argparse.Namespace) -> None:
    mem = _memory(args)
    out = mem.get(args.text) if args.id else \
        mem.search(args.text, include_inactive=args.all, limit=args.limit)
    if args.json:
        _print_json(out)
    elif args.id:
        _print_item(Term(), out)
    else:
        _print_results(Term(), args.text, out)
    if not args.id and mem.vector_note:
        print(f"nazareth: keyword search only ({mem.vector_note})", file=sys.stderr)


def _print_results(t: Term, text: str, out: dict[str, Any]) -> None:
    if not any(out.get(k) for k in ("records", "questions", "events", "facts")):
        print(f"Nothing found for {text!r}.")
        return
    sections = []
    if out["records"]:
        lines = []
        for r in out["records"]:
            head = f"  {t.id(r['id'])}  {r['kind']}"
            if r.get("topic"):
                head += f"  [{r['topic']}]"
            if r["status"] != "active":
                head += "  " + t.status(r["status"])
                if r.get("current"):
                    head += t.meta(f" → current {r['current']}")
            lines += [head, *t.wrap(r["text"], 6, lines=3)]
        sections.append(("RECORDS", lines))
    if out["questions"]:
        lines = []
        for q in out["questions"]:
            lines.append(f"  {t.id(q['id'])}  {t.status(q['status'])}")
            lines += t.wrap(q["question"] + (f" → {q['answer']}" if q.get("answer") else ""),
                            6, lines=3)
        sections.append(("QUESTIONS", lines))
    if out["events"]:
        lines = []
        for e in out["events"]:
            lines.append(f"  {t.id(e['id'])}  " + _event_line(t, e).lstrip())
        sections.append(("EVENTS", lines))
    if out.get("facts"):
        lines = []
        for f in out["facts"]:
            wrapped = t.wrap(f["fact"], 4, lines=3)
            lines.append("  • " + wrapped[0].lstrip())
            lines += wrapped[1:]
            if f["sources"]:
                lines.append(t.meta("    from ") + ", ".join(t.id(s) for s in f["sources"]))
        sections.append(("FACTS (knowledge graph, may be imprecise)", lines))
    for i, (title, lines) in enumerate(sections):
        print(("\n" if i else "") + t.heading(title))
        print("\n".join(lines))


def _print_item(t: Term, item: dict[str, Any]) -> None:
    """One record, question or event in full."""
    kind = item.get("kind") or ("question" if "question" in item else "event")
    head = [t.id(item["id"]), kind]
    if item.get("topic"):
        head.append(f"[{item['topic']}]")
    if item.get("status"):
        head.append(t.status(item["status"]))
    print("  ".join(head))
    by = item.get("actor") or item.get("asked_by")
    print(t.meta("by ") + t.actor(by) + t.meta(f" · {_when(item['ts'])}"))
    print()
    body = item.get("text") or item.get("question") or item.get("summary", "")
    print("\n".join(t.wrap(body, 0)))
    for label, key in (("Why", "rationale"), ("Context", "context"), ("Answer", "answer")):
        if item.get(key):
            print("\n" + t.heading(label))
            print("\n".join(t.wrap(item[key], 0)))
    links = [("status", item.get("status_reason")), ("ref", item.get("ref")),
             ("severity", item.get("severity")), ("answered by", item.get("answered_by"))]
    for label, key in (("supersedes", "supersedes"), ("superseded by", "superseded_by"),
                       ("current", "current"), ("depends on", "depends_on"),
                       ("dependents", "dependents")):
        v = item.get(key)
        if v:
            links.append((label, ", ".join(t.id(x) for x in ([v] if isinstance(v, str) else v))))
    shown = [(k, v) for k, v in links if v]
    if shown:
        print()
        for k, v in shown:
            print(t.meta(f"{k:>13}: ") + str(v))


def cmd_history(args: argparse.Namespace) -> None:
    out = _memory(args).history(since=args.since, actor=args.actor, action=args.action,
                                limit=args.limit)
    if args.json:
        _print_json(out)
        return
    t = Term()
    for e in out["entries"]:
        print(_event_line(t, e))


def cmd_log(args: argparse.Namespace) -> None:
    out = _memory(args).log(args.action, args.summary, actor=HUMAN, ref=args.ref)
    print(out["id"])


# -- parser -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nazareth", description="Agent-native project memory")
    parser.add_argument("--dir", help="project directory (default: nearest .nazareth or git root)")
    sub = parser.add_subparsers(dest="command", metavar="command")

    def add(name: str, fn, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create .nazareth/ in this project")
    p.add_argument("--claude", action="store_true",
                   help="also add lens subagents, the MCP server and a context hook for Claude Code")
    p.add_argument("--docker", action="store_true",
                   help="also add a Docker Agent team config with a coordinator and the lenses")
    p.add_argument("--model", metavar="PROVIDER/MODEL",
                   help="model for the Docker Agent lenses (default: first with credentials)")
    add("serve", cmd_serve, "run the MCP memory server (stdio)")
    add("status", cmd_status, "what needs you, what's decided, what happened").add_argument(
        "--json", action="store_true")
    add("context", cmd_context, "memory snapshot for agent session start (hook)")
    add("verify", cmd_verify, "check the ledger hash chain")
    add("rebuild", cmd_rebuild, "rebuild the index from the ledger and reset vectors")
    add("embed", cmd_embed, "compute semantic search vectors now (needs the search extra)")
    add("enrich", cmd_enrich,
        "send new ledger events to the knowledge graph (graph extra; calls your LLM)"
        ).add_argument("--limit", type=int, help="send at most this many events")

    p = add("decide", cmd_decide, "record a decision as the human")
    p.add_argument("topic")
    p.add_argument("decision")
    p.add_argument("--why", required=True)
    p.add_argument("--supersedes", nargs="+", metavar="ID")
    p.add_argument("--depends-on", nargs="+", metavar="ID")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = add("constrain", cmd_constrain, "add a non-negotiable constraint")
    p.add_argument("constraint")
    p.add_argument("--why", required=True)
    p.add_argument("--topic")
    p.add_argument("--supersedes", nargs="+", metavar="ID")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = add("answer", cmd_answer, "answer an open question")
    p.add_argument("question_id")
    p.add_argument("answer")

    p = add("resolve", cmd_resolve, "accept or reject a pending record")
    p.add_argument("record_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true")
    g.add_argument("--reject", dest="accept", action="store_false")
    p.add_argument("--note")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = add("retire", cmd_retire, "take an active decision or constraint out of effect")
    p.add_argument("record_id")
    p.add_argument("--why", required=True)

    p = add("phase", cmd_phase, "show or set the project phase")
    p.add_argument("phase", nargs="?", help="set the current phase (same as --current)")
    p.add_argument("--current")
    p.add_argument("--next")

    p = add("query", cmd_query, "search, or read one item with --id")
    p.add_argument("text", help="search text, or an id with --id")
    p.add_argument("--id", action="store_true", help="treat text as an id")
    p.add_argument("--all", action="store_true", help="include superseded/retired/rejected")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true", help="print JSON")

    p = add("history", cmd_history, "show the ledger")
    p.add_argument("--since")
    p.add_argument("--actor")
    p.add_argument("--action")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")

    p = add("log", cmd_log, "log an event as the human (e.g. a directive)")
    p.add_argument("action")
    p.add_argument("summary")
    p.add_argument("--ref")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return
    try:
        args.fn(args)
    except NazarethError as e:
        print(f"nazareth: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        while _opened:
            _opened.pop().close()


if __name__ == "__main__":
    main()
