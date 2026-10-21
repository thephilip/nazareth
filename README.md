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
- **Fixed identities.** A server started with `NAZARETH_ACTOR` writes as that actor and
  ignores the tools' `actor` argument, and nobody writes as `human` over MCP: the human
  writes through the CLI. A lens can't pose as another lens or as you.
- **Deterministic and fast.** No LLM sits on the write path; a call takes milliseconds.
- **Safe with many writers.** Each lens runs its own server process; appends take a file
  lock and re-read the chain head, so concurrent writers never fork it.

## Quick start

```bash
uv tool install -e .   # or: pipx install -e .  (puts `nazareth` on PATH)
cd your-project
nazareth init          # creates .nazareth/ (ledger + derived index)
nazareth status
```

### Semantic search (optional)

```bash
uv tool install -e '.[search]'   # or: pip install 'nazareth[search]'
nazareth embed                   # optional: compute vectors now instead of on first query
```

The `search` extra installs [model2vec](https://github.com/MinishLab/model2vec) and numpy.
`query` then ranks results by meaning as well as keywords (FTS5 and vector hits fused by
reciprocal rank), so "where do lenses get their identity" finds the decision about
`NAZARETH_ACTOR`. The first use downloads the `minishlab/potion-base-8M` model (~30 MB)
from Hugging Face into its usual cache; after that it loads from the cache without network
access. Vectors live in `.nazareth/vectors.db`, are computed lazily at query time (never on
the write path), and are deleted by `rebuild`.

Without the extra, or if the model can't be loaded (for example offline on first use),
`query` quietly uses keyword search alone. The CLI prints one line to stderr when that
happens; agents just get keyword results.

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
  the phase, constraints, active decisions and anything waiting on you at startup, resume,
  clear and compaction. Long lists are trimmed with a pointer to `query`, so the
  pending-work warning always fits.

It checks `.mcp.json` and `.claude/settings.json` before writing anything; if either isn't
valid JSON, or isn't shaped as expected, it stops and changes nothing.

Claude Code runs the bare `nazareth` command for the hook and every server, so it must be
on your PATH, not only inside a virtualenv. `init --claude` warns if it's missing or if
PATH has a different installation. Commit the generated files so a fresh clone gets the
lenses and hook; `.claude/settings.local.json` is per-user and stays out of git.

Lens subagents' own servers only load in folders you've trusted in Claude Code, and Claude
Code asks you to approve `.mcp.json` servers the first time.

### Docker Agent

```bash
brew install docker-agent      # if you don't have it yet
nazareth init --docker         # optionally --model provider/model; combines with --claude
docker agent run .docker-agent/nazareth.yaml             # the coordinator routes for you
docker agent run .docker-agent/nazareth.yaml -a lens-code # or pick a lens yourself
```

This adds, without overwriting anything that already exists:

- `.docker-agent/nazareth.yaml`: one team config with a coordinator (`root`) and the three
  lenses. Every agent runs its own `nazareth serve` with a fixed `NAZARETH_ACTOR`
  (`agent:main`, `lens:architect`, `lens:code`, `lens:reviewer`). The architect has no
  shell; the architect, reviewer and coordinator get read-only filesystem and git. The
  coordinator gets the `nazareth context` snapshot at session start.
- `.docker-agent/coordinator.md` and `.docker-agent/lenses/lens-*.md`: the prompts. The
  lens prompts are the same text the Claude Code subagents use.

Routing is plain delegation: `root` hands each request to one lens with Docker Agent's
`transfer_task`, choosing by the lens descriptions, unless you ask for a sequence such as
design, then implement, then review. The memory write tools (`decide`, `constrain`,
`ask`, `log`, `phase`) are auto-approved; the read tools are annotated read-only.

All agents share one model, `lens`. By default it's a `first_available` list (Anthropic,
then OpenAI, Google, and the local Docker Model Runner), so it uses whichever credentials
you have. `--model provider/model` writes a single model instead. To use any
OpenAI-compatible endpoint, such as a company LLM gateway, uncomment the `providers:`
example in the generated file. It reads `NAZARETH_LLM_BASE_URL` and `NAZARETH_LLM_API_KEY`
from the environment, so no URL or key lands in the file. For example, keep the key in
the macOS keychain and export it when you launch:
`NAZARETH_LLM_API_KEY=$(security find-generic-password -s <name> -w) docker agent run …`.

Like Claude Code, Docker Agent runs the bare `nazareth`, so it must be on your PATH.

Other hosts: run `nazareth serve` as a stdio MCP server and set `NAZARETH_ACTOR`.

## CLI (you are actor `human`)

| Command | Purpose |
|---|---|
| `init [--claude] [--docker [--model M]]` | Create `.nazareth/`; add the Claude Code and/or Docker Agent integration |
| `status` | What needs you, constraints, decisions, recent activity |
| `answer <q-id> "..."` | Answer a lens's question |
| `resolve <id> --accept\|--reject` | Settle a pending decision |
| `decide`, `constrain` | Record your own decisions and constraints |
| `retire <id> --why` | Take a decision or constraint out of effect |
| `phase [X] [--next Y]` | Show or set the project phase (`--current X` also works) |
| `query <text>` / `query --id <id>` | Search, or read one item with its supersession chain |
| `history`, `log` | Read the ledger; log a directive |
| `verify` | Check the hash chain |
| `rebuild` | Regenerate the index from the ledger and reset vectors |
| `embed` | Compute semantic search vectors now (needs the `search` extra) |
| `serve` | Run the MCP server (stdio) |
| `context` | Memory snapshot for agent session start (used by the hook) |

## MCP tools (for lenses)

| Tool | Purpose |
|---|---|
| `brief` | Phase, constraints, active decisions, pending items, questions, recent activity |
| `query` | Search (keyword, plus semantic with the `search` extra), or read one item by id |
| `decide` | Record a decision; `supersedes` replaces earlier ones |
| `constrain` | Record a non-negotiable constraint |
| `ask` | Leave a question for the human |
| `log` | Record an artifact, finding, test, … |
| `phase` | Read or set the project phase |
| `history` | Read the ledger |

`brief`, `query` and `history` carry the MCP read-only hint; the others are marked
non-destructive.

## Environment

| Variable | Description | Default |
|---|---|---|
| `NAZARETH_PROJECT_DIR` | Project root | nearest `.nazareth/`, else git root, else the start dir (`CLAUDE_PROJECT_DIR` under Claude Code, otherwise cwd) |
| `NAZARETH_ACTOR` | Fixed actor for a server's writes; the tools' `actor` argument is ignored when set, and `human` is refused | `agent` |

## State layout

```
.nazareth/
├── ledger.jsonl    # source of truth (hash-chained, append-only)
├── index.db        # SQLite projection: records, links, questions, FTS (derived, gitignored)
├── vectors.db      # embeddings for semantic search (derived, gitignored; `search` extra)
└── .gitignore
```

## Known limitations

- **Lenses with a shell can write as you.** The CLI records every write as `human`, and
  the Claude Code lens subagents have Bash (and the Docker Agent code and reviewer lenses
  have a shell), so a misbehaving or prompt-injected lens could run
  `nazareth decide …` and skip the pending-review gate. MCP writes are protected; the CLI
  isn't yet. An environment-variable guard was considered and rejected (agents can unset
  it, and it would block your own `!` commands); a confirmation an agent can't fake is
  being explored. Tracked as q-0bfe36.
- **Docker Agent `--sandbox` is untested.** The generated config expects `nazareth` on the
  host PATH; inside a sandbox it may not be there.
- **`rebuild` while servers are running.** Other running servers keep searching the
  deleted vector store until they restart. Results stay correct; restart them to free it.

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## Roadmap

1. ~~Concurrency-safe ledger~~ · ~~decision store~~ · ~~human CLI~~
2. ~~Claude Code lens definitions~~; dogfood on nazareth itself
3. ~~Semantic search: local model2vec embeddings, hybrid with FTS5~~
4. ~~Docker Agent lenses~~, routing by coordinator delegation (`transfer_task`). Routing
   hooks and choice evaluators were left out: delegation covers the MVP, and evaluators
   need extra hosted endpoints. A model router comes later
5. Graphiti as an optional, asynchronous enrichment layer
