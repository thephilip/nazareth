# Changelog

All notable changes to Nazareth are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Entries are added under `[Unreleased]` in the
same change as the code.

## [Unreleased]

### Changed

- `enrich` warns about missing facts only for decisions, constraints and questions, which
  always have something to extract. Routine events and commits often don't, so their
  "no facts" is shown plainly on the progress line, without the warning.
- `enrich` hides Graphiti's warnings about LLM slips it corrects itself (such as invalid
  `duplicate_facts` indexes); its errors still show, and `--verbose` shows everything.

## [0.2.1] - 2026-10-23

### Added

- `NAZARETH_LLM_REASONING_EFFORT` sends `reasoning_effort` with every `enrich` request.
  With GLM 5.3, `low` cut reasoning from about 470 tokens to 6 per prompt: events took
  10–16 s with facts instead of about 50 s, and the empty responses stopped.
- `NAZARETH_ENRICH_TIMEOUT` (default 600 s) caps the time one event may take, including
  Graphiti's retries. `enrich` then stops with a message instead of retrying for many
  minutes, and the events before it stay saved.

### Fixed

- Ctrl-C during `enrich` stops quietly: the in-flight LLM calls are cancelled, the graph is
  closed, and it says the events already sent are saved, instead of printing a traceback.
  Other commands exit with a one-line "interrupted" (status 130).
- `enrich` no longer tells you to install the graph extra when only the LLM settings are
  missing, and the install hints for both extras now give commands that work: the
  installer with `--extras`, or `uv tool install -e … --force` from a checkout, instead of
  `pip install 'nazareth[…]'` (nazareth isn't on PyPI, and pip would install elsewhere).

## [0.2.0] - 2026-10-22

### Added

- `install.sh` for macOS, Linux and WSL (`curl -LsSf …/install.sh | sh`): installs the
  latest release with uv, offering to install uv first, with `--extras`, `--core`,
  `--version`, `--uninstall`, and a guard against replacing a development install. CI
  runs shellcheck and real installs on Ubuntu, macOS and a bare Debian container.

- Commit logging: in a git repository, `init` adds a `post-commit` hook that runs
  `nazareth record-commit`, logging each commit's subject, short sha and a few files as a
  `commit` event by actor `git`. It never fails a commit and leaves existing hooks and
  `core.hooksPath` setups alone. The `git` actor is refused over MCP.
- `init --claude` adds a `PostToolUse` hook running `nazareth nudge`, which reminds the
  agent to record its decisions after a `git commit` when nothing has been recorded since
  the previous one.

### Changed

- The session-start snapshot and the MCP server instructions ask agents to record each
  decision as soon as it's settled, not at the end of the task.

### Fixed

- `init --claude` adds `permissions.allow` rules for the memory servers' tools
  (`mcp__nazareth`, and `mcp__lens-memory` for the lenses). Without them Claude Code's auto
  mode refused agents' `decide` and other writes, so nothing got recorded. Rerun
  `init --claude` in existing projects to add the rules and the new hook.

## [0.1.0] - 2026-10-22

The first release.

### Added

- `nazareth --version`.
- CI on GitHub Actions: the test suite without any extra on Python 3.12–3.14, and with
  every extra plus ruff. A `test` extra holds the test-only dependencies.
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

- The CLI prints readable text instead of JSON for `query`, `query --id`, `decide`,
  `constrain` and `resolve`, with `--json` for the old output. Output is coloured on a
  terminal (`NO_COLOR` and `FORCE_COLOR` are honoured) and wraps to its width.
- `enrich` prints one line per event (time taken and facts found) instead of a counter,
  so progress also shows in logs and background runs, and it names the events that
  produced no facts, which usually means the model failed to extract them.
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

- `rebuild` empties the vector store in place instead of deleting the file, so running
  lens servers see the reset rather than carrying on with an orphaned copy. A corrupt
  store is still deleted.
- The test suite runs without the optional extras, so the keyword-only fallback is
  tested in a plain install.
- `verify` says "1 entry", not "1 entries".
- `init --docker` prints `docker-agent run` when Homebrew's standalone binary is
  installed (the `docker agent` form only exists with Docker Desktop's plugin), and the
  README uses it too.
- The Docker Agent coordinator no longer re-logs what a lens already recorded.
- The `graph` extra now installs `httpx`, which graphiti-core imports without declaring,
  and an incomplete extra gives an actionable error instead of a traceback.
- CLI commands and `nazareth serve` close their memory on exit, so a `query` that read the
  knowledge graph no longer leaves its embedded database server running.
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

[Unreleased]: https://github.com/thephilip/nazareth/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/thephilip/nazareth/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/thephilip/nazareth/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/thephilip/nazareth/releases/tag/v0.1.0
