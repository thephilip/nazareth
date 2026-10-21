# Nazareth

Agent-native shared memory for local agentic coding workflows.

## What is this?

Long agent sessions drift: the *why* behind earlier choices gets lost, stale decisions get
re-litigated, and nobody notices two agents quietly disagreeing. Nazareth gives every agent
"lens" (architect, code, reviewer) and the human one shared, append-only project memory:
decisions with rationale, constraints, open questions, and a log of what happened.

It is built for agents first. Lenses talk to it over MCP; the human steers from the CLI.

## Three perspectives

- **Human, near the loop.** `nazareth status` shows what needs you (pending decisions,
  open questions), what's in effect, and what happened. You answer, resolve, and set
  direction; you don't review every line.
- **Lenses.** Stateless agents that call `brief` to orient, `decide`/`constrain` to record
  choices, `ask` instead of guessing, and `log` what they produce.
- **Ledger.** A hash-chained, append-only JSONL file. It is the single source of truth;
  everything else is derived from it and can be rebuilt.

## How it stays honest

- **Explicit supersession.** A decision replaces an earlier one only by naming its id.
  Superseding something already replaced fails and names the record now in effect, so an
  agent working from stale context is corrected instead of forking history.
- **Conflicts go to the human.** A decision on a topic that already has an active
  decision, without superseding it, is held as *pending*. So is a lens trying to override
  something the human decided.
- **Deterministic and fast.** No LLM sits on the write path; a call takes milliseconds.
- **Safe with many writers.** Each lens runs its own server process; appends take a file
  lock and re-read the chain head, so concurrent writers never fork it.

## Quick start

```bash
pip install -e .
cd your-project
nazareth init          # creates .nazareth/ (ledger + derived index)
nazareth status
```

### Claude Code

```bash
nazareth init --claude
```

This adds, without overwriting anything that already exists:

- `.claude/agents/lens-{architect,code,reviewer}.md`: lens subagents. Each runs its own
  `nazareth serve` with a fixed `NAZARETH_ACTOR`, and is denied the main session's
  nazareth tools so it can't write under the wrong identity.
- `.mcp.json`: the `nazareth` server for the main session (actor `agent:main`).
- `.claude/settings.json`: a `SessionStart` hook running `nazareth context`, which injects
  the phase, constraints and active decisions at startup, resume, clear and compaction.

Lens subagents' own servers only load in folders you've trusted in Claude Code, and Claude
Code asks you to approve `.mcp.json` servers the first time.

Other hosts: run `nazareth serve` as a stdio MCP server and set `NAZARETH_ACTOR`.

## CLI (you are actor `human`)

| Command | Purpose |
|---|---|
| `init [--claude]` | Create `.nazareth/`; `--claude` adds the Claude Code integration |
| `status` | What needs you, constraints, decisions, recent activity |
| `answer <q-id> "..."` | Answer a lens's question |
| `resolve <id> --accept\|--reject` | Settle a pending decision |
| `decide`, `constrain` | Record your own decisions and constraints |
| `retire <id> --why` | Take a decision or constraint out of effect |
| `phase [--current X] [--next Y]` | Show or set the project phase |
| `query <text>` / `query --id <id>` | Search, or read one item with its supersession chain |
| `history`, `log` | Read the ledger; log a directive |
| `verify` | Check the hash chain |
| `rebuild` | Regenerate the index from the ledger |
| `serve` | Run the MCP server (stdio) |
| `context` | Memory snapshot for agent session start (used by the hook) |

## MCP tools (for lenses)

| Tool | Purpose |
|---|---|
| `brief` | Phase, constraints, active decisions, pending items, questions, recent activity |
| `query` | Keyword search, or read one item by id |
| `decide` | Record a decision; `supersedes` replaces earlier ones |
| `constrain` | Record a non-negotiable constraint |
| `ask` | Leave a question for the human |
| `log` | Record an artifact, finding, test, … |
| `phase` | Read or set the project phase |
| `history` | Read the ledger |

## Environment

| Variable | Description | Default |
|---|---|---|
| `NAZARETH_PROJECT_DIR` | Project root | nearest `.nazareth/`, else git root, else cwd |
| `NAZARETH_ACTOR` | Default actor for a server's writes | `agent` |

## State layout

```
.nazareth/
├── ledger.jsonl    # source of truth (hash-chained, append-only)
├── index.db        # SQLite projection: records, links, questions, FTS (derived, gitignored)
└── .gitignore
```

## Roadmap

1. ~~Concurrency-safe ledger~~ · ~~decision store~~ · ~~human CLI~~
2. ~~Claude Code lens definitions~~; dogfood on nazareth itself
3. Semantic search: LanceDB + local embeddings, projected from the ledger
4. Docker Agent lenses (`lenses/` predates the current design and needs rewriting),
   hook-based routing (built-in `task_route` or a Laya classifier)
5. Graphiti as an optional, asynchronous enrichment layer
