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

WIDTH = 96


def _clip(text: str, width: int = WIDTH) -> str:
    text = " ".join(text.splitlines()).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def _when(iso: str | None) -> str:
    if not iso:
        return ""
    dt = datetime.fromisoformat(iso)
    fmt = "%H:%M" if dt.date() == datetime.now().date() else "%b %d %H:%M"
    return dt.strftime(fmt)


def _paths(args: argparse.Namespace) -> StatePaths:
    return StatePaths(Path(args.dir).resolve() if args.dir else find_project_dir())


def _memory(args: argparse.Namespace) -> Memory:
    return Memory(_paths(args))


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2))


def _record_line(r: dict[str, Any]) -> str:
    topic = f"[{r['topic']}] " if r.get("topic") else ""
    return f"  {r['id']}  {_clip(topic + r['text'], WIDTH - 14)}"


# -- commands -----------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> None:
    from nazareth.claude_code import install, path_warning

    paths = _paths(args)
    # install() checks the project's Claude Code config before writing, so a
    # bad file stops init before anything (including .nazareth/) is created.
    report = install(paths.project) if args.claude else []
    existed = paths.initialized
    paths.init()
    Memory(paths).close()
    print(f"{'Already initialized' if existed else 'Initialized'}: {paths.state}")
    for path, what in report:
        print(f"  {path}: {what}")
    if args.claude and (warning := path_warning(sys.argv[0])):
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
    mem.index.rebuild()
    print(f"Rebuilt index from {len(mem.ledger)} ledger entries.")


def cmd_status(args: argparse.Namespace) -> None:
    mem = _memory(args)
    b = mem.brief()
    if args.json:
        _print_json(b)
        return
    valid, chain = mem.ledger.verify()
    ph = b["phase"]
    print(f"nazareth · {mem.paths.project}")
    print(f"Phase: {ph['current']} → next: {ph['next']}")
    print(f"Ledger: {chain}" + ("" if valid else "  ⚠ run `nazareth verify`"))

    pending, open_q = b.get("pending_review", []), b.get("open_questions", [])
    print()
    if pending or open_q:
        print(f"NEEDS YOU ({len(pending) + len(open_q)})")
        for r in pending:
            print(_record_line(r) + "   (pending)")
            print(f"      why pending: {_clip(r['reason'], WIDTH - 19)}")
        for q in open_q:
            print(f"  {q['id']}  {_clip(q['question'], WIDTH - 12)}")
            meta = f"asked by {q['asked_by']} {_when(q['ts'])}"
            if q.get("context"):
                meta += f" · {q['context']}"
            print(f"      {_clip(meta, WIDTH - 6)}")
        print("  → nazareth answer <q-id> \"...\"   ·   nazareth resolve <id> --accept|--reject")
    else:
        print("NEEDS YOU: nothing")

    for title, rows in (("CONSTRAINTS", b["constraints"]), ("DECISIONS", b["decisions"])):
        print(f"\n{title} ({len(rows)})")
        for r in rows:
            print(_record_line(r))
    if b.get("decisions_truncated"):
        print(f"  … {b['decisions_truncated']}")

    if b.get("recent_answers"):
        print("\nRECENT ANSWERS")
        for q in b["recent_answers"][:5]:
            print(f"  {q['id']}  {_clip(q['question'] + ' → ' + q['answer'], WIDTH - 12)}")

    print("\nRECENT ACTIVITY")
    for e in reversed(b["recent_activity"]):
        line = f"{_when(e['ts']):>12}  {e['actor']:<16} {e['action']:<10} {e['summary']}"
        print(f"  {_clip(line, WIDTH)}")


def cmd_decide(args: argparse.Namespace) -> None:
    out = _memory(args).decide(args.topic, args.decision, args.why, actor=HUMAN,
                               supersedes=args.supersedes, depends_on=args.depends_on)
    _print_json(out)


def cmd_constrain(args: argparse.Namespace) -> None:
    out = _memory(args).constrain(args.constraint, args.why, actor=HUMAN, topic=args.topic,
                                  supersedes=args.supersedes)
    _print_json(out)


def cmd_answer(args: argparse.Namespace) -> None:
    out = _memory(args).answer(args.question_id, args.answer)
    print(f"{out['id']}: answered")


def cmd_resolve(args: argparse.Namespace) -> None:
    out = _memory(args).resolve(args.record_id, accept=args.accept, note=args.note)
    _print_json(out)


def cmd_retire(args: argparse.Namespace) -> None:
    out = _memory(args).retire(args.record_id, args.why)
    print(f"{out['id']}: retired")


def cmd_phase(args: argparse.Namespace) -> None:
    mem = _memory(args)
    if args.current or args.next:
        ph = mem.set_phase(HUMAN, current=args.current, next_phase=args.next)
    else:
        ph = mem.phase()
    print(f"{ph['current']} → next: {ph['next']}")


def cmd_query(args: argparse.Namespace) -> None:
    mem = _memory(args)
    if args.id:
        _print_json(mem.get(args.text))
    else:
        _print_json(mem.search(args.text, include_inactive=args.all, limit=args.limit))


def cmd_history(args: argparse.Namespace) -> None:
    out = _memory(args).history(since=args.since, actor=args.actor, action=args.action,
                                limit=args.limit)
    if args.json:
        _print_json(out)
        return
    for e in out["entries"]:
        ref = f" ({e['ref']})" if e.get("ref") else ""
        line = f"{_when(e['ts']):>12}  {e['actor']:<16} {e['action']:<10} {e['summary']}{ref}"
        print(_clip(line, WIDTH + 30))


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

    add("init", cmd_init, "create .nazareth/ in this project").add_argument(
        "--claude", action="store_true",
        help="also add lens subagents, the MCP server and a context hook for Claude Code")
    add("serve", cmd_serve, "run the MCP memory server (stdio)")
    add("status", cmd_status, "what needs you, what's decided, what happened").add_argument(
        "--json", action="store_true")
    add("context", cmd_context, "memory snapshot for agent session start (hook)")
    add("verify", cmd_verify, "check the ledger hash chain")
    add("rebuild", cmd_rebuild, "rebuild the index from the ledger")

    p = add("decide", cmd_decide, "record a decision as the human")
    p.add_argument("topic")
    p.add_argument("decision")
    p.add_argument("--why", required=True)
    p.add_argument("--supersedes", nargs="+", metavar="ID")
    p.add_argument("--depends-on", nargs="+", metavar="ID")

    p = add("constrain", cmd_constrain, "add a non-negotiable constraint")
    p.add_argument("constraint")
    p.add_argument("--why", required=True)
    p.add_argument("--topic")
    p.add_argument("--supersedes", nargs="+", metavar="ID")

    p = add("answer", cmd_answer, "answer an open question")
    p.add_argument("question_id")
    p.add_argument("answer")

    p = add("resolve", cmd_resolve, "accept or reject a pending record")
    p.add_argument("record_id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--accept", action="store_true")
    g.add_argument("--reject", dest="accept", action="store_false")
    p.add_argument("--note")

    p = add("retire", cmd_retire, "take an active decision or constraint out of effect")
    p.add_argument("record_id")
    p.add_argument("--why", required=True)

    p = add("phase", cmd_phase, "show or set the project phase")
    p.add_argument("--current")
    p.add_argument("--next")

    p = add("query", cmd_query, "search, or read one item with --id")
    p.add_argument("text", help="search text, or an id with --id")
    p.add_argument("--id", action="store_true", help="treat text as an id")
    p.add_argument("--all", action="store_true", help="include superseded/retired/rejected")
    p.add_argument("--limit", type=int, default=10)

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


if __name__ == "__main__":
    main()
