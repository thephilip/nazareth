# Nazareth

Agent-native memory layer with temporal knowledge graph, hash-chain ledger, and lens orchestration.

## Project Structure

- `src/nazareth/` — Python package
  - `server.py` — MCP memory server (7 tools: decide, constrain, query, log, supersede, phase, history)
  - `cli.py` — CLI entry point (`nazareth serve`, `nazareth verify`)
  - `ledger/chain.py` — Hash-chained append-only event log
  - `memory/graph.py` — Graphiti + FalkorDB wrapper
- `lenses/` — Docker Agent YAML definitions (architect, code, reviewer, root orchestrator)
- `tests/` — pytest test suite

## Development

```bash
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Dependencies

- FalkorDB (via docker-compose) for the temporal knowledge graph
- Graphiti for entity extraction and supersession detection
- LanceDB for vector search
- MCP for the agent-facing tool interface

## Key Design Decisions

- Lenses are stateless perspectives that query shared state, not persistent workers
- The ledger is tamper-evident (SHA256 hash chain) — `nazareth verify` checks integrity
- Graphiti handles supersession automatically via LLM-driven entity extraction
- All MCP tool calls are self-audited (logged to the ledger)
