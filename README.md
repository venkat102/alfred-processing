# Alfred Processing App

AI agent orchestration service for Frappe customizations. Runs CrewAI agents
that design, generate, validate, and deploy Frappe DocTypes, scripts, and
workflows against a live customer site over MCP.

## Architecture

- **FastAPI** WebSocket server - one connection per active conversation.
- **AgentPipeline state machine** (`alfred/api/pipeline.py`) - 10 named phases
  over a shared `PipelineContext`: `sanitize` → `load_state` → `plan_check`
  → `orchestrate` → `enhance` → `clarify` → `resolve_mode` → `build_crew`
  → `run_crew` → `post_crew`. Each phase is unit-testable in isolation,
  auto-wrapped in a tracer span, and can abort via `ctx.stop(error, code)`.
- **Three-mode chat orchestrator** (`alfred/orchestrator.py`,
  feature-flagged via `ALFRED_ORCHESTRATOR_ENABLED`) - classifies every
  prompt into `dev` / `plan` / `insights` / `chat`. Conversational and
  read-only turns short-circuit the crew entirely, so *"hi"*, *"what
  DocTypes do I have?"*, and *"how would we approach this?"* don't
  trigger the full SDLC pipeline. Plan mode produces a reviewable plan
  doc the user can approve and promote to Dev via a single click.
- **CrewAI** crews: Dev mode runs the full SDLC (Full or Lite tier),
  Plan mode runs a 3-agent planning crew (`alfred/agents/plan_crew.py`,
  Requirement → Assessment → Architect, terminal task
  `generate_plan_doc`), and Insights mode runs a 1-agent read-only crew
  (`alfred/agents/crew.py::build_insights_crew`). Chat mode has no crew
  at all - just a single LLM call.
  - **Full Dev**: 6-agent SDLC (Requirement → Assessment → Architect →
    Developer → Tester → Deployer), sequential process, ~5-10 min per
    task.
  - **Lite Dev**: single-agent fast pass, ~1 min per task, ~5x cheaper
    - for simple customizations.
- **MCP client** (`alfred/tools/mcp_client.py`) - sends JSON-RPC requests
  to the client-app MCP server (12 tools) over the same WebSocket so agents
  query the live Frappe site during reasoning. Uses `run_coroutine_threadsafe`
  to dispatch from CrewAI's synchronous tool-invocation threads back to the
  main async loop.
- **Phase 1 MCP hardening** - every tool call goes through a per-run wrapper
  that enforces a budget cap, dedupes identical calls, counts failures, and
  exposes a fast-path Redis cache keyed by `(conversation_id, tool, args)`.
  Reduces token burn 15-30% on repeat-heavy workloads.
- **Phase 2 handoff condenser** - `alfred/agents/condenser.py` attaches a
  `Task.callback` to each upstream crew task that compacts `task_output.raw`
  in place before the next task reads it. Strips prose + markdown fences +
  tail-truncates fallback. `generate_changeset` is deliberately exempted so
  the changeset artifact survives unchanged. ~60-70% handoff context reduction
  with zero extra LLM calls.
- **Phase 2 conversation memory** - `alfred/state/conversation_memory.py`
  persists items / clarifications / recent prompts per-conversation in Redis
  and renders them into the prompt enhancer's user message on follow-up turns,
  so "now add X to that DocType" resolves against the earlier work without
  the user respelling it.
- **Phase 3 think-then-act** - `generate_changeset` + `LITE_TASK_DESCRIPTION`
  force the Developer to emit a numbered 1-6 item PLAN in its first Thought
  before calling any tool. The plan stays in reasoning; Final Answer is raw
  JSON only.
- **Phase 3 reflection minimality** (`alfred/agents/reflection.py`,
  feature-flagged via `ALFRED_REFLECTION_ENABLED`) - small LLM call that drops
  items the user didn't ask for. Safety net refuses to strip all items.
- **Phase 3 tracing** (`alfred/obs/tracer.py`, enabled via
  `ALFRED_TRACING_ENABLED`) - zero-dep async-safe span tracer. Writes one
  JSONL object per finished phase to `ALFRED_TRACE_PATH` (default
  `./alfred_trace.jsonl`). Optional stderr summary via `ALFRED_TRACE_STDOUT`.
  Call-site API matches OpenTelemetry's context manager so switching to a
  real OTel SDK later is mechanical.
- **Pre-preview dry-run** - after the crew produces a changeset, the pipeline
  calls `dry_run_changeset` via MCP to validate everything against the live
  DB. DDL-triggering doctypes (DocType, Custom Field, Workflow, Property
  Setter) route through a meta-only path that never calls `.insert()` to
  avoid MariaDB implicit commits during DDL; savepoint-safe doctypes
  (Notification, Server Script, Client Script, ...) use savepoint + rollback.
  Failed validations trigger one automatic self-heal retry with just the
  Developer agent.
- **Activity streaming** - every MCP tool call fires an `agent_activity`
  WebSocket event so the browser UI shows concrete progress instead of a
  silent spinner.

## Quick Start

```bash
cp .env.example .env
# Edit .env: set API_SECRET_KEY and LLM configuration
docker compose up -d
```

## Development

```bash
# Native dev (requires Python 3.11, Redis on 13000)
./dev.sh

# Run tests
.venv/bin/python -m pytest tests/ -q

# Full suite minus the state store (needs live Redis)
.venv/bin/python -m pytest tests/ --ignore=tests/test_state_store.py -q

# Standalone LLM connectivity check
.venv/bin/python test_llm.py
```

## Feature Flags (environment variables)

| Variable | Default | Effect |
|---|---|---|
| `ALFRED_ORCHESTRATOR_ENABLED` | off | Enable the three-mode chat orchestrator (chat / insights / plan / dev routing, plan doc generation, cross-mode handoff). Fixes the "hi" → "Unable to classify prompt intent" bug as a side effect. |
| `ALFRED_REFLECTION_ENABLED` | off | Enable the minimality reflection step |
| `ALFRED_TRACING_ENABLED` | off | Enable structured pipeline tracing |
| `ALFRED_TRACE_PATH` | `./alfred_trace.jsonl` | JSONL output path |
| `ALFRED_TRACE_STDOUT` | off | Also emit a stderr summary per span |
| `ALFRED_PHASE1_DISABLED` | off | Disable the Phase 1 MCP tracking state (benchmark use only) |

See the [Setup Guide](../frappe-bench/apps/alfred_client/docs/SETUP.md),
[Admin Guide](../frappe-bench/apps/alfred_client/docs/admin-guide.md), and
[Architecture](../frappe-bench/apps/alfred_client/docs/architecture.md) for
full instructions.

## License

MIT
