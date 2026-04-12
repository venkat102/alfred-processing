# Alfred Processing App

AI agent orchestration service for Frappe customizations. Runs CrewAI agents that design, generate, validate, and deploy Frappe DocTypes, scripts, and workflows.

## Architecture

- **FastAPI** WebSocket server - one connection per active conversation
- **CrewAI** agent pipeline in two modes:
  - **Full**: 6-agent SDLC (Requirement → Assessment → Architect → Developer → Tester → Deployer), sequential process, ~5-10 min per task
  - **Lite**: single-agent fast pass, ~1 min per task, ~5× cheaper - for simple customizations
- **MCP client** (`alfred/tools/mcp_client.py`) - sends JSON-RPC requests to the client-app MCP server over the same WebSocket so agents can query the live Frappe site (schemas, permissions, existing customizations, dry-run validation). Uses `run_coroutine_threadsafe` to dispatch from CrewAI's synchronous tool-invocation threads back to the main async loop.
- **Pre-preview dry-run** - after the crew produces a changeset, the pipeline calls `dry_run_changeset` via MCP to validate everything against the live DB via savepoint rollback. Failed validations trigger one automatic self-heal retry with only the Developer agent.
- **Activity streaming** - every MCP tool call fires an `agent_activity` WebSocket event so the browser UI shows concrete progress instead of a silent spinner.

## Quick Start

```bash
cp .env.example .env
# Edit .env: set API_SECRET_KEY and LLM configuration
docker compose up -d
```

## Development

```bash
# Native dev (requires Python 3.11, Redis on 6379)
./dev.sh

# Run tests
.venv/bin/python -m pytest tests/ -q

# Full suite minus the state store (needs live Redis)
.venv/bin/python -m pytest tests/ --ignore=tests/test_state_store.py -q

# Standalone LLM connectivity check
.venv/bin/python test_llm.py
```

See the [Setup Guide](../frappe-bench/apps/alfred_client/docs/SETUP.md) and
[Architecture](../frappe-bench/apps/alfred_client/docs/architecture.md) for full instructions.

## License

MIT
