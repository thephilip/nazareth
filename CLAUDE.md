# Nazareth

Agent-native shared project memory: hash-chained ledger as source of truth, SQLite projection
for decisions/constraints/questions, MCP server for agent lenses, CLI for the human.

## Project Structure

- `src/nazareth/` — Python package
  - `ledger/chain.py` — Hash-chained append-only log; file-locked appends, safe across processes
  - `memory/projection.py` — SQLite index derived from the ledger (records, links, questions, FTS5)
  - `memory/service.py` — `Memory`: all reads/writes and the write policy (supersession, pending)
  - `memory/vectors.py` — Optional semantic search (`search` extra): `Embedder` protocol,
    model2vec embedder, `VectorStore` over `.nazareth/vectors.db` (derived, lazily synced at
    query time), reciprocal rank fusion with FTS5
  - `memory/graph.py` — Optional knowledge graph (`graph` extra): ledger events as Graphiti
    episodes on embedded FalkorDB Lite (`.nazareth/graph.db`), written only by
    `nazareth enrich`; graph facts feed `query`'s rank fusion
  - `server.py` — MCP server (8 tools: brief, query, decide, constrain, ask, log, phase, history)
  - `cli.py` — Human CLI (`nazareth status`, `answer`, `resolve`, …)
  - `term.py` — CLI styling: colour roles (NO_COLOR/FORCE_COLOR), terminal width, wrapping
  - `paths.py` — Finds the project's `.nazareth/` state dir
  - `claude_code.py` — `init --claude` installer, `nazareth context` (SessionStart hook output)
    and the post-commit `nudge`
  - `git_hook.py` — the git `post-commit` hook `init` installs, and `record-commit`
  - `docker_agent.py` — `init --docker` installer: Docker Agent team config and lens prompts
  - `templates/lenses/` — Host-neutral lens prompts (single source for both hosts)
  - `templates/claude/` — Claude Code frontmatter per lens; `templates/docker/` — team
    config template and coordinator prompt
- `.nazareth/` — Nazareth's own project memory (dogfooding). `ledger.jsonl` is committed;
  run `nazareth status` to see decisions and open questions
- `install.sh` — POSIX installer (macOS, Linux, WSL) over `uv tool install`; CI's install
  jobs run it for real, `tests/test_install_script.py` checks its arguments
- `tests/` — pytest suite; `tests/data/` holds the vendored docker-agent schema

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"   # includes the search and graph extras; tests use fakes, no network
pytest
ruff check src tests
```

The core package must work without any extra. CI (`.github/workflows/ci.yml`) runs the
suite with only `.[test]` on Python 3.12–3.14 and with `.[dev]` plus ruff. Tests that need
the search extra are marked `@needs_search` (from `tests/conftest.py`); graph tests that
need the real extra use `needs_graph`.

Keep `README.md` and `CHANGELOG.md` (under `## [Unreleased]`) current in the same change as
the code: user-visible behaviour, commands, dependencies and known limitations.

## Key Design Decisions

- The ledger is the only source of truth. `index.db` is a projection: never write to it
  directly; `nazareth rebuild` must always reproduce it.
- Write-time policy outcomes (active vs. pending) are stored in the event, so replay is faithful
  even if the policy changes later.
- No LLM or network calls on the write path. Enrichment (embeddings, Graphiti) must be derived
  and asynchronous.
- Every lens runs its own `nazareth serve` process. Anything that checks state and then writes
  must do so inside `ledger.transaction()` after `index.sync()`.
- Agent-facing errors raise `NazarethError` with an actionable message; the server converts them
  to MCP `ToolError` (other exceptions reach the agent only as a generic failure).
- MCP SDK is 2.x (`mcp.server.mcpserver.MCPServer`, snake_case result fields).
- Reads are not logged to the ledger; state changes are, and that is the audit trail.
