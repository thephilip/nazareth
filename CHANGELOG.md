# Changelog

All notable changes to Nazareth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Entries are added under `[Unreleased]` in the
same change as the code.

## [Unreleased]

Nothing has been released yet. Everything below will become 0.1.0.

### Added

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

- Rebuilt the memory layer ledger-first. The first scaffold's Graphiti/FalkorDB, LanceDB
  and gateway-specific configuration were dropped. Graphiti may return later as optional
  asynchronous enrichment, and LLM providers stay pluggable.
- `init` adds missing patterns to an existing `.nazareth/.gitignore`, and the vector store
  does the same when it first creates `vectors.db`, so older projects ignore it too.

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
  Writing as the human through the CLI from a lens's shell is still possible; see Known
  limitations in the README.
