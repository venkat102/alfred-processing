# Alfred Processing App

AI agent orchestration service for Frappe customizations. Runs CrewAI agents
that design, generate, validate, and deploy Frappe DocTypes, scripts, and
workflows against a live customer site over MCP.

## Architecture

- **FastAPI** WebSocket server - one connection per active conversation.
- **AgentPipeline state machine** (`alfred/api/pipeline.py`) - 15 named phases
  over a shared `PipelineContext`: `sanitize` → `load_state` → `warmup`
  → `plan_check` → `orchestrate` → `classify_intent` → `classify_module`
  → `enhance` → `clarify` → `inject_kb` → `resolve_mode`
  → `provide_module_context` → `build_crew` → `run_crew` → `post_crew`.
  Each phase is unit-testable in isolation, auto-wrapped in a tracer span,
  and can abort via `ctx.stop(error, code)`. The `warmup` phase pre-pulls
  distinct models across the triage / reasoning / agent tiers with
  `keep_alive=10m` to eliminate the cold-load penalty when a pipeline hits
  each tier. `classify_intent`, `classify_module`, and
  `provide_module_context` are flag-gated Dev-mode additions that select
  per-intent Builder specialists and inject per-module domain context into
  the Developer's prompt.
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
- **Per-intent Builder specialists** (`alfred/agents/builders/`,
  feature-flagged via `ALFRED_PER_INTENT_BUILDERS`) - the generic Developer
  is swapped for a specialist Agent when the prompt matches a known intent.
  Each specialist carries a domain-focused backstory plus a registry-driven
  checklist of shape-defining fields the output must include, with defaults
  layered by a post-crew backfill. Shipped specialists: **DocType Builder**
  (`create_doctype`) and **Report Builder** (`create_report`). Registry
  files live at `alfred/registry/intents/*.json` - adding a new intent is
  one JSON file plus a builder module. Missing fields are surfaced as
  editable "default" pills in the client preview (see `field_defaults_meta`
  on `ChangesetItem`).
- **Module specialists** (`alfred/agents/specialists/module_specialist.py`,
  feature-flagged via `ALFRED_MODULE_SPECIALISTS`) - cross-cutting advisers
  invoked twice per build. `provide_context` runs before the Developer and
  injects module-specific conventions (roles, naming patterns, gotchas) as
  a prompt addendum. `validate_output` runs after the crew emits its
  changeset and returns domain-correctness notes (deterministic rules +
  LLM pass, deduped). Shipped as data: 13 module KBs at
  `alfred/registry/modules/*.json` - Accounts, Assets, Buying, CRM, Custom,
  HR, Maintenance, Manufacturing, Payroll, Projects, Selling, Stock,
  Support. Provide-context calls are cached in Redis (falls back to
  in-memory) with a 5-minute TTL.
- **Module family layer** (`alfred/registry/modules/_families/`, activated
  whenever `ALFRED_MODULE_SPECIALISTS=1`) - 4 family KBs sit above the 13
  module KBs and carry cross-module invariants shared by their member
  modules: **Transactions** (accounts + selling + buying), **Operations**
  (stock + manufacturing + assets), **People** (hr + payroll),
  **Engagement** (crm + support + projects + maintenance). `custom` is
  intentionally familyless. Before the Developer runs, the specialist
  fetches a FAMILY CONTEXT snippet (15-minute Redis cache) and prepends it
  above the MODULE CONTEXT snippet, so the intent specialist sees
  `PRIMARY FAMILY (X)` + `PRIMARY MODULE (Y)` + `SECONDARY MODULE CONTEXT
  (Z)` sections. Frappe family builders' backstories acknowledge both
  layers as authoritative with a family-invariant-wins precedence rule -
  this is how Frappe agents communicate with ERPNext agents for domain
  knowledge, validation, and verification.
- **Multi-module classification** (feature-flagged via `ALFRED_MULTI_MODULE`,
  layers on top of module specialists) - heuristic classifier detects a
  primary module plus up to 2 secondaries for cross-domain prompts (e.g.
  *"Sales Invoice that auto-creates a Project task"* → primary=accounts,
  secondaries=[projects]). Primary module's validation notes keep full
  severity; secondary modules' blockers are capped to warning so only
  primary-module concerns can gate deploy. Primary wins the naming pattern;
  permissions are merged deduped across all detected modules. Secondary
  modules in the SAME family as the primary reuse the primary's FAMILY
  section (no duplicate family header).
- **Insights → Report handoff** (feature-flagged via `ALFRED_REPORT_HANDOFF`)
  - when an Insights reply represents a report-shaped query (tabular,
  aggregation-ready), the handler attaches a structured `ReportCandidate`
  the client uses to render a "Save as Report" button. Clicking fires a
  Dev-mode turn with a `__report_candidate__` JSON trailer that the
  pipeline parses to short-circuit intent classification to
  `create_report` (no re-interpretation). Mode classifier's fast-path is
  tightened so analytics verbs (*"show top N"*, *"list the top"*,
  *"summary of"*, *"report on"*) route to Insights by default, while
  explicit deploy verbs (*"build a report"*, *"create a report"*) still
  win and route directly to Dev.
- **MCP client** (`alfred/tools/mcp_client.py`) - sends JSON-RPC requests
  to the client-app MCP server (14 tools) over the same WebSocket so agents
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
  `./alfred_trace.jsonl`). Output path is validated against a permitted-root
  whitelist (CWD, $HOME, tempfile dir, `/tmp`, `/var/tmp`); `..` traversal
  and paths outside the whitelist fall back to the default with a WARNING.
  Optional stderr summary via `ALFRED_TRACE_STDOUT`. Call-site API matches
  OpenTelemetry's context manager so switching to a real OTel SDK later is
  mechanical.
- **Pre-preview dry-run** - after the crew produces a changeset, the pipeline
  calls `dry_run_changeset` via MCP to validate everything against the live
  DB. DDL-triggering doctypes (DocType, Custom Field, Workflow, Property
  Setter) route through a meta-only path that never calls `.insert()` to
  avoid MariaDB implicit commits during DDL; savepoint-safe doctypes
  (Notification, Server Script, Client Script, ...) use savepoint + rollback.
  Failed validations trigger one automatic self-heal retry with just the
  Developer agent.
- **Multi-model tiers** (`alfred/llm_client.py`) - standalone LLM calls
  (classifier, chat, reflection, enhancer, clarifier, rescue) and CrewAI
  agents each resolve a model from one of three tiers (`triage`, `reasoning`,
  `agent`). Each tier is configured from Alfred Settings; empty fields fall
  back to the default model, so existing single-model deployments keep
  working unchanged. All standalone calls go through urllib rather than
  litellm to sidestep the httpcore/anyio read-hang under thread executors.
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

| Variable | Default | Depends on | Effect |
|---|---|---|---|
| `ALFRED_ORCHESTRATOR_ENABLED` | off | none | Enable the three-mode chat orchestrator (chat / insights / plan / dev routing, plan doc generation, cross-mode handoff). Fixes the "hi" → "Unable to classify prompt intent" bug as a side effect. |
| `ALFRED_PER_INTENT_BUILDERS` | off | none | Swap the generic Developer agent for a specialist Agent based on the classified intent (`create_doctype`, `create_report`). Surfaces "default" pills in the preview via `field_defaults_meta`. |
| `ALFRED_MODULE_SPECIALISTS` | off | `ALFRED_PER_INTENT_BUILDERS=1` | Inject per-module domain context into the specialist's prompt pre-build and run a domain-validation pass post-crew. Emits a module badge and validation-notes section in the preview. |
| `ALFRED_MULTI_MODULE` | off | `ALFRED_MODULE_SPECIALISTS=1` | Detect primary + up to 2 secondary modules per prompt. Secondary-module blockers are capped to warning so only primary blockers gate deploy. |
| `ALFRED_REPORT_HANDOFF` | off | `ALFRED_PER_INTENT_BUILDERS=1` | Insights handler emits a structured `report_candidate` for report-shaped queries; client renders a "Save as Report" button; pipeline short-circuits intent classification to `create_report` on handoff. Also tightens mode classifier fast-path so analytics verbs route to Insights. |
| `ALFRED_REFLECTION_ENABLED` | off | none | Enable the minimality reflection step |
| `ALFRED_TRACING_ENABLED` | off | none | Enable structured pipeline tracing |
| `ALFRED_TRACE_PATH` | `./alfred_trace.jsonl` | `ALFRED_TRACING_ENABLED=1` | JSONL output path. Validated against a permitted-root whitelist (CWD, $HOME, `tempfile.gettempdir()`, `/tmp`, `/var/tmp`); paths with `..` or outside the whitelist log a WARNING and fall back to the default. |
| `ALFRED_TRACE_STDOUT` | off | `ALFRED_TRACING_ENABLED=1` | Also emit a stderr summary per span |
| `ALFRED_FKB_DIR` | auto-detected | none | Override the path to the shared Frappe knowledge base (`frappe_kb/`) directory. Normally auto-located by walking from `alfred_processing/alfred/knowledge/fkb.py` up to `bench/apps/alfred_client/alfred_client/data/frappe_kb`. Set this when running CI or non-standard bench layouts where the auto-locate walk doesn't find the sibling app. |
| `ALFRED_PHASE1_DISABLED` | off | none | Disable the Phase 1 MCP tracking state (benchmark use only) |

### Specialist-feature flag stack

The four V1-V4 flags form a layered stack. Each flag is a strict extension
of the layer below - turning a higher flag on without the one it depends
on is effectively a no-op (logged at startup). Behaviour at each layer:

| Flags on | Behaviour |
|---|---|
| none | Pre-V1 Alfred (generic Developer, no specialists) |
| `PER_INTENT_BUILDERS` | V1: DocType + Report specialists swap in based on intent |
| `PER_INTENT_BUILDERS + MODULE_SPECIALISTS` | V2: single-module context injection + validation |
| `PER_INTENT_BUILDERS + MODULE_SPECIALISTS + MULTI_MODULE` | V3: primary + up to 2 secondary modules |
| any of the above + `REPORT_HANDOFF` | V4: Insights→Report handoff button + structured classification |

At startup, Alfred logs the flag state:
`Module-specialist flags: ALFRED_PER_INTENT_BUILDERS=ON/OFF ALFRED_MODULE_SPECIALISTS=ON/OFF ALFRED_MULTI_MODULE=ON/OFF`
(The report handoff flag is logged at first handoff invocation rather than startup.)

See the [Setup Guide](../frappe-bench/apps/alfred_client/docs/SETUP.md),
[Admin Guide](../frappe-bench/apps/alfred_client/docs/admin-guide.md), and
[Architecture](../frappe-bench/apps/alfred_client/docs/architecture.md) for
full instructions.

## License

MIT
