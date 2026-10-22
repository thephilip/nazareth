# Changelog

All notable changes to Nazareth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Entries are added under `[Unreleased]` in the
same change as the code.

## [Unreleased]

Nothing has been released yet. Everything below will become 0.1.0.

### Added

- An optional knowledge graph: the `graph` extra adds Graphiti on embedded FalkorDB Lite.
  `nazareth enrich [--limit N]` sends new ledger events to any OpenAI-compatible LLM
  (`NAZARETH_LLM_BASE_URL`, `NAZARETH_LLM_API_KEY`, `NAZARETH_LLM_MODEL`) to extract facts.
  `query` then ranks graph facts alongside keywords and vectors and returns them, with
  their source ids, under `facts`. The graph lives in a derived `.nazareth/graph.db`,
  deleted by `rebuild`, and no LLM is called anywhere else.
- `nazareth init --docker [--model provider/model]`, which writes a Docker Agent team
  config (`.docker-agent/nazareth.yaml`) with a coordinator that delegates to the
  architect, code and reviewer lenses. Each agent runs its own memory server with a fixed
  identity, and the model defaults to the first of Anthropic, OpenAI, Google and the
  local Docker Model Runner that has credentials.
- MCP tool annotations: `brief`, `query` and `history` are read-only, and the write tools
  are marked non-destructive.
- Hybrid semantic search as an optional `search` extra: model2vec
  (`minishlab/potion-base-8M`) embeddings fused with FTS5 keyword search by reciprocal
  rank. Vectors live in a derived `.nazareth/vectors.db`, are computed lazily at query
  time and never on the write path, and are reset by `rebuild`.
- `nazareth embed`, which computes semantic search vectors ahead of the first query.
- `nazareth init --claude`, which installs architect, code and reviewer lens subagents,
  `.mcp.json`, and a `SessionStart` hook that runs `nazareth context`.
- `nazareth context`, a memory snapshot for agent session start that always keeps the
  pending-work warning within its size budget.
- `nazareth phase X` sets the phase directly, alongside `--current X`.
- Ledger-first memory: a hash-chained, append-only JSONL ledger as the only source of
  truth, with decisions, constraints and questions projected into a rebuildable SQLite
  index with FTS5 search.
- An MCP server (`nazareth serve`) with `brief`, `query`, `decide`, `constrain`, `ask`,
  `log`, `phase` and `history`.
- A human CLI: `status`, `answer`, `resolve`, `decide`, `constrain`, `retire`, `query`,
  `history`, `log`, `verify` and `rebuild`.
- Explicit supersession: a decision replaces another only by naming its id. Conflicting
  decisions, and lenses overriding the human, are held as pending for the human.
- Nazareth's own project memory in `.nazareth/ledger.jsonl` (dogfooding).

### Changed

- Lens prompts have a single source shared by Claude Code and Docker Agent, and no
  longer name host-specific tools. Rerun `init --claude` after deleting
  `.claude/agents/lens-*.md` to pick up the new wording.
- The PATH warning from `init` now covers both hosts.
- `rebuild` also deletes the knowledge graph, and the dev extra now includes the graph
  extra.
- Rebuilt the memory layer ledger-first. The first scaffold's Graphiti/FalkorDB, LanceDB
  and gateway-specific configuration were dropped. Graphiti may return later as optional
  asynchronous enrichment, and LLM providers stay pluggable.
- `init` adds missing patterns to an existing `.nazareth/.gitignore`, and the vector store
  does the same when it first creates `vectors.db`, so older projects ignore it too.

### Removed

- The top-level `lenses/` Docker Agent files, which predated the current design.

### Fixed

- Parallel tool calls from one agent no longer fail with a generic error after their write
  had already been recorded.
- The first search no longer makes the same lens's writes wait for the model download.
- A transient error during semantic search falls back to keyword search for that query
  only, instead of disabling semantic search until restart.
- `init --claude` checks existing `.mcp.json` and `.claude/settings.json` before writing
  anything and writes files atomically.
- `nazareth context` prints a short note instead of a traceback when memory is unreadable,
  and `init --claude` warns when `nazareth` isn't on PATH.

### Security

- A server started with `NAZARETH_ACTOR` ignores the tools' `actor` argument, and
  `human` is refused over MCP, so a lens can't pose as another lens or as the human.
- `init --claude` adds `permissions.deny` rules, and the Docker Agent config denies the
  shell, for the CLI commands that write as the human (`decide`, `constrain`, `answer`,
  `resolve`, `retire`, `phase`, `log`), so agents can't write as the human by accident.
  The rules match command text, so a deliberate bypass is still possible; see Known
  limitations in the README.
