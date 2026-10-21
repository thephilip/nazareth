# Nazareth

Agent-native memory layer and lens orchestration for local agentic coding workflows.

## What is this?

Nazareth is a framework for running specialized AI agent "lenses" (architect, code, reviewer) that share a persistent, temporally-aware knowledge graph. Instead of each agent session starting from scratch or drifting from project goals, every lens queries and writes to shared state — decisions are tracked with rationale, supersession is automatic, and a tamper-evident ledger records everything that happened.

## Architecture

```
Human (steering) ──directives──→ Shared State ←──queries── Lens agents
                                      │
                  ┌───────────────────┼───────────────────┐
                  │                   │                   │
          Temporal Graph        JSONL Ledger        Vector Index
          (Graphiti +           (hash-chained,      (LanceDB)
           FalkorDB)            append-only)
```

### Three Layers

1. **Temporal Knowledge Graph** (Graphiti + FalkorDB) — decisions and constraints as graph nodes with automatic supersession detection. Agents query "what's currently valid?" not "what was ever written."

2. **Hash-Chain Ledger** — append-only JSONL log with SHA256 chain. Every action, decision, and finding is recorded immutably. Verifiable with `nazareth verify`.

3. **Vector Index** (LanceDB) — semantic search over decisions and history. "Find me everything related to auth" without loading the full graph.

### Lenses (Docker Agent)

Lenses are specialized AI agents defined as YAML files:

- **architect** — makes design decisions, sets constraints, doesn't write code
- **code** — implements features, reads decisions before coding, logs artifacts
- **reviewer** — reviews changes against decisions/constraints, logs findings

Each lens connects to the memory layer via MCP and runs in Docker Agent containers for isolation.

## Quick Start

```bash
# Start FalkorDB
docker compose up -d

# Install nazareth
pip install -e .

# Set the LLM gateway key (if not already in keychain)
export LLM_GATEWAY_KEY=$(security find-generic-password -a $USER -s llm-gateway -w)

# Run the MCP server
nazareth serve

# Verify ledger integrity
nazareth verify

# Run a lens via Docker Agent
docker agent run lenses/agent.yaml
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `NAZARETH_PROJECT_DIR` | Project root directory | `.` |
| `FALKORDB_HOST` | FalkorDB host | `localhost` |
| `FALKORDB_PORT` | FalkorDB port | `6379` |

## MCP Tools

The memory server exposes these tools to agent lenses:

| Tool | Description |
|---|---|
| `decide` | Record a decision with rationale (auto-supersedes) |
| `constrain` | Add a non-negotiable project constraint |
| `query` | Natural language query against the knowledge graph |
| `log` | Append an event to the ledger |
| `supersede` | Explicitly replace a prior decision |
| `phase` | Get/set current project phase |
| `history` | Query the ledger by time, actor, or action |
