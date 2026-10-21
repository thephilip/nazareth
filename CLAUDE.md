# Nazareth

Agent-native shared project memory: hash-chained ledger as source of truth, SQLite projection
for decisions/constraints/questions, MCP server for agent lenses, CLI for the human.

## Project Structure

- `src/nazareth/` — Python package
  - `ledger/chain.py` — Hash-chained append-only log; file-locked appends, safe across processes
  - `memory/projection.py` — SQLite index derived from the ledger (records, links, questions, FTS5)
  - `memory/service.py` — `Memory`: all reads/writes and the write policy (supersession, pending)
  - `server.py` — MCP server (8 tools: brief, query, decide, constrain, ask, log, phase, history)
  - `cli.py` — Human CLI (`nazareth status`, `answer`, `resolve`, …)
  - `paths.py` — Finds the project's `.nazareth/` state dir
  - `claude_code.py` — `init --claude` installer and `nazareth context` (SessionStart hook output)
  - `templates/claude/` — Lens subagent definitions installed into projects
- `.nazareth/` — Nazareth's own project memory (dogfooding). `ledger.jsonl` is committed;
  run `nazareth status` to see decisions and open questions
- `lenses/` — Docker Agent YAML (stale; predates current design, to be rewritten)
- `tests/` — pytest suite

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check src tests
```

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
