# Changelog

All notable changes to the Alfred project (processing app + client app +
admin portal). Dates are ISO-8601. Entries are grouped by the scope tag
in brackets: `[processing]`, `[client]`, `[admin]`, or `[all]` for
cross-cutting work.

The format is based on Keep a Changelog, but we track phases rather than
semver since Alfred is still pre-1.0.

---

## Unreleased

### Added
- `[all]` **Plan mode + cross-mode handoff (Phase C of three-mode chat)**.
  When the orchestrator classifies a prompt as `plan` (e.g. *"how would we
  approach adding approval to Expense Claims?"*), the pipeline
  short-circuits through a new handler that runs a 3-agent planning crew
  (Requirement Analyst, Feasibility Assessor, Solution Architect) and
  produces a **structured plan document** instead of a changeset. No DB
  writes. The plan is rendered as a rich panel in the chat with Refine
  and Approve & Build buttons. Clicking Approve & Build sends a canned
  "Approve and build the plan" prompt with `mode=dev`, which triggers a
  Dev-mode run with the plan injected as a CONTEXT block.
  - New `alfred/models/plan_doc.py` with `PlanDoc` + `PlanStep` Pydantic
    models (title / summary / steps / doctypes_touched / risks /
    open_questions / estimated_items). `PlanDoc.stub()` for fallback
    paths.
  - New `alfred/agents/plan_crew.py::build_plan_crew()`. Reuses the
    existing Requirement / Assessment / Architect agents from
    `alfred.agents.definitions` so Plan and Dev pipelines share the
    same agent definitions. The terminal task is `generate_plan_doc`
    with a description that mandates strict JSON output matching the
    PlanDoc schema. Handoff condenser callbacks apply to the first
    two tasks; the terminal task output is kept verbatim.
  - New `alfred/handlers/plan.py::handle_plan()`. Builds the plan crew,
    inits per-run MCP tracking with a moderate budget (15, between
    insights at 5 and dev at 30), runs via the existing `run_crew`
    machinery, strips code fences, parses the first well-formed JSON
    object via `JSONDecoder.raw_decode`, validates against `PlanDoc`,
    falls back to a stub doc on any failure. Never raises.
  - `ConversationMemory` extended with `plan_documents` (capped list,
    cap 5) and richer `active_plan` handling. New methods
    `add_plan_document(plan, status)` and `mark_active_plan_status(status)`
    maintain the `proposed → approved → built` lifecycle. Approved
    plans render their full step list in `render_for_prompt()` so the
    Dev enhancer sees them verbatim; proposed and built plans render
    summary only.
  - `PipelineContext.plan_doc` field. New `_run_plan_short_circuit()`
    helper emits the `plan_doc` WebSocket message, records the plan
    as proposed, and persists memory.
  - **Plan → Dev handoff** wired into `_phase_enhance`:
    `_maybe_approve_active_plan()` detects approval phrasing
    (*"Approve and build the plan"*, *"build it"*, etc.) and flips the
    active plan status to `"approved"` BEFORE rendering the memory
    context block. `_mark_active_plan_built_if_any()` runs after the
    Dev changeset is produced so a plan isn't re-injected on the next
    Dev turn.
  - New frontend `PlanDocPanel.vue` component renders plan docs as a
    structured panel with status badge (Proposed / Approved / Built /
    Rejected), numbered step list, doctypes-touched chips, risks,
    open questions, and Refine / Approve & Build action buttons. The
    buttons are hidden once the plan has been approved or built.
  - `MessageBubble.vue` renders the new `plan_doc` message type via
    `PlanDocPanel`. `AlfredChatApp.vue` subscribes to the new
    `alfred_plan_doc` realtime event, handles `plan-refine` (drops a
    suggested prompt into the input) and `plan-approve` (sends the
    canned approval prompt with `mode=dev`).
  - `websocket_client.py` event map extended with `plan_doc →
    alfred_plan_doc`. New `_store_plan_doc_message()` persists plans as
    `Alfred Message` rows with `metadata.mode="plan"` and the full plan
    JSON in `metadata.plan` so scrollback survives page reload.
  - +27 new tests across `test_plan_crew.py` (8),
    `test_plan_handler.py` (14), `test_conversation_memory.py` (+9 for
    plan_documents / approved plan rendering), `test_pipeline_state_machine.py`
    (+7 for plan short-circuit and Plan → Dev handoff).

- `[all]` **UI mode switcher + conversation mode persistence
  (Phase D of three-mode chat)**. Users can now explicitly pick a chat
  mode per conversation via a 4-button switcher in the chat header
  (Auto / Dev / Plan / Insights). Auto is the default and lets the
  orchestrator decide; the other three force a specific mode for every
  prompt on that conversation.
  - New `ModeSwitcher.vue` component. `v-model` binds to the current
    mode; emits `update:modelValue` on every click. Tooltips explain
    what each mode does.
  - New `Alfred Conversation.mode` Select field (Auto / Dev / Plan /
    Insights, default Auto). Persisted via a new
    `alfred_chat.set_conversation_mode(conversation, mode)` whitelisted
    endpoint with permission check on the conversation.
  - `get_conversations()` now includes `mode` in the returned fields
    so the frontend can restore the sticky preference when switching
    conversations.
  - `AlfredChatApp.vue` adds a `currentMode` ref that loads from the
    conversation row on `openConversation()` and persists back via a
    watcher whenever the user clicks a different mode. The initial
    load does NOT trigger a write back.
  - `sendMessage(text, modeOverride)` signature extended with an
    optional per-call mode override so Plan-doc "Approve and Build"
    can force `dev` for that one turn without flipping the sticky
    preference.

- `[processing]` **Insights mode (Phase B of three-mode chat)**. When the
  orchestrator classifies a prompt as `insights` (e.g. *"what DocTypes do
  I have?"*, *"which workflows are active?"*), the pipeline short-circuits
  through a new handler that runs a single-agent CrewAI crew with a
  **read-only MCP tool subset** and returns a markdown answer. No
  changeset is produced, nothing writes to the DB, and the tool budget is
  hard-capped at 5 calls per turn.
  - New module `alfred/handlers/insights.py` with `handle_insights()`.
    Builds the crew, inits per-run MCP tracking with the tight insights
    budget, runs via the existing `run_crew` machinery, strips any
    leading/trailing code fences, returns markdown.
  - New `alfred/agents/crew.py::build_insights_crew()` mirrors
    `build_lite_crew` but with a "Frappe Site Information Specialist"
    role, a markdown-output task (`generate_insights_reply`), and no JSON
    output requirement.
  - `build_mcp_tools()` now returns an `"insights"` key with a read-only
    tool subset: `lookup_doctype`, `lookup_pattern`, `get_site_info`,
    `get_doctypes`, `get_existing_customizations`, `get_user_context`,
    `check_permission`, `has_active_workflow`, `check_has_records`,
    `validate_name_available`. Deploy-shaped `dry_run_changeset` and the
    local Python/JS/ask_user stubs are explicitly excluded.
  - New orchestrator fast-path prefixes for common read-only query
    phrasings (*"what X do I have?"*, *"which X..."*, *"show me my..."*,
    *"do I have..."*, *"how many..."*) so obvious Insights prompts skip
    the classifier LLM call entirely.
  - `ConversationMemory` extended with `insights_queries` (capped Q/A
    log) and `active_plan` (forward-compat with Phase C). The context
    block now renders insights queries and active plan so follow-up
    Plan/Dev turns can reference *"that workflow I asked about"*.
  - `PipelineContext.insights_reply` field plus a new
    `_run_insights_short_circuit` helper in `pipeline.py` that emits
    the `insights_reply` message type and records the Q/A in memory.
  - Client-side `websocket_client.py` event map extended with
    `chat_reply`, `insights_reply`, `mode_switch` event names; both reply
    types are stored as `Alfred Message` rows (with `metadata.mode`) so
    scrollback survives page reload.
  - `MessageBubble.vue` renders `chat_reply`, `insights_reply`, and
    `mode_switch` types, plus a small `modeBadge` next to every agent
    message so users can see which mode produced the reply.
  - `AlfredChatApp.vue` subscribes to the three new realtime events
    (`alfred_chat_reply`, `alfred_insights_reply`, `alfred_mode_switch`)
    and pushes them into the message list with the correct typing.
  - +32 new tests across `test_insights_handler.py` (10),
    `test_insights_crew.py` (10), `test_orchestrator.py` (+3 for insights
    fast-path), `test_pipeline_state_machine.py` (+2 for insights
    short-circuit), `test_conversation_memory.py` (+7 for
    insights_queries / active_plan).

- `[processing]` **Three-mode chat orchestrator (Phase A)**. New `orchestrate`
  phase in the pipeline state machine classifies every prompt into one of
  four modes: `dev` (run the agent crew, current behavior), `plan` (produce
  a plan doc, wired in Phase C), `insights` (read-only Q&A, wired in
  Phase B), or `chat` (conversational reply, no crew). Gated by the
  `ALFRED_ORCHESTRATOR_ENABLED=1` feature flag on the processing app - when
  off the pipeline behaves exactly as before.
  - New module `alfred/orchestrator.py` with `classify_mode()`: manual
    override beats fast-path beats LLM classification beats confidence-based
    fallback. Fast-path handles exact greetings and imperative build verbs
    without an LLM call. LLM call uses the same litellm pattern as
    `enhance_prompt` (low temp, small max_tokens, JSON-shaped output).
  - New module `alfred/handlers/chat.py` with `handle_chat()`: single LLM
    call, conversation memory rendered into system prompt, no tools bound.
    Never raises - returns a static fallback on any error.
  - `PipelineContext` extended with `mode`, `manual_mode_override`,
    `orchestrator_reason`, `orchestrator_source`, `chat_reply` fields.
    Every dev-only phase (`enhance`, `clarify`, `resolve_mode`,
    `build_crew`, `run_crew`, `post_crew`) early-returns when
    `ctx.mode != "dev"`.
  - `websocket.py` prompt handler parses a `mode` field from incoming
    prompt messages and threads it through `_run_agent_pipeline` as
    `manual_mode`. Valid values: `auto` (default) / `dev` / `plan` /
    `insights`.
  - `alfred_chat.send_message(conversation, message, mode="auto")` accepts
    a `mode` parameter and includes it in the Redis prompt payload.
  - New message types emitted from the pipeline: `mode_switch` (fired on
    every orchestrator decision for UI badges), `chat_reply` (emitted by
    the chat handler short-circuit).
  - +34 new tests across `test_orchestrator.py` (23) and
    `test_chat_handler.py` (11), plus 12 new tests in
    `test_pipeline_state_machine.py` covering the orchestrate phase and
    mode gating on every downstream phase.

### Fixed
- `[processing]` **Sanitizer "Unable to classify prompt intent" hard block
  on greetings.** `alfred/defense/sanitizer.py:check_prompt` previously
  returned `allowed=False` for any prompt whose keyword classifier returned
  `unknown`. This rejected `"hi"`, `"thanks"`, and any prompt without a
  Frappe keyword, producing the misleading "Flagged for admin review" error.
  The sanitizer now only hard-blocks real injection patterns
  (`DEFAULT_INJECTION_PATTERNS`); unknown intents pass through with
  `needs_review=True` for logging only. Also added common greetings
  (`hi`/`hello`/`thanks`/etc.) to `classify_intent` so they classify as
  `general_question` before reaching the fallback. Fixes the original
  motivating bug for the three-mode chat feature.

### Documentation
- `[all]` Full doc rewrite covering Phase 1/2/3 additions: new sections in
  `architecture.md`, `developer-api.md`, `admin-guide.md`, `user-guide.md`,
  `debugging.md`. Tool count corrected from 9 to 12 across every doc that
  referenced it. Expanded the processing-app README + admin-portal README
  (previously ~15 lines). New `CHANGELOG.md`, `SECURITY.md`,
  `benchmarking.md`, `operations.md`, `data-model.md`.

### Security
- `[client]` Authorization audit fix: added `frappe.has_permission` owner
  checks on `approve_changeset`, `reject_changeset`, `get_changeset`,
  `get_latest_changeset`, `apply_changeset`, `rollback_changeset`,
  `send_message` (CLI helper), `start_conversation`, `stop_conversation`,
  `escalate_conversation`. Previously these only ran `validate_alfred_access()`
  (a coarse role gate), so any user with the Alfred role could read / approve
  / deploy / rollback another user's changeset.
- `[client]` `start_conversation` now boots the connection manager under the
  **conversation owner's** session, not the caller's. Previously, opening
  a shared conversation would run every MCP tool call as the caller, leaking
  whatever data the caller could see into the agent's world view.
- `[client]` `get_escalated_conversations` is now System-Manager-only.
- `[admin]` `subscribe_to_plan` + `cancel_subscription` now require
  System Manager via `_require_billing_admin()`. They use
  `ignore_permissions=True` internally, so without a role gate any logged-in
  admin-portal user could mutate any customer's plan.

### Fixed
- `[client]` **Dry-run committed Workflow changes to the DB.** The savepoint
  path called `frappe.get_doc(Workflow, ...).insert()`, which triggered
  `Workflow.on_update()` → `Custom Field.save()` → `ALTER TABLE` →
  MariaDB implicit commit, killing the savepoint. Rewrote `_dry_run_single`
  to route DDL-triggering doctypes (DocType, Custom Field, Property Setter,
  Workflow, Workflow State, Workflow Action Master, DocField) through a
  meta-only path that never calls `.insert()` or `.validate()`.
- `[client]` `_execute_rollback` had a dead `op == "create"` branch (nothing
  populates rollback_data with that shape). Removed.
- `[client]` `test_mcp.py` expected 9 tools but `TOOL_REGISTRY` has 12
  (`dry_run_changeset`, `lookup_doctype`, `lookup_pattern` added
  post-Phase-1). Test would fail on run. Updated the expected set and
  added smoke tests for the three new tools.
- `[admin]` `Alfred Plan` DocType had no `pipeline_mode` field, and
  `check_plan()` never returned it. The tier-locked pipeline-mode override
  feature was completely non-functional. Added the field (Select:
  `full`/`lite`, default `full`) + updated `check_plan` to return it in
  every response branch.
- `[admin]` `check_trial_expirations` crashed on fresh installs where the
  `Alfred Admin Settings` singleton didn't exist yet. Wrapped in try/except
  with a 7-day grace period fallback.
- `[processing]` `_extract_changes` couldn't handle qwen retry loops that
  produced 5+ concatenated JSON arrays with `<|im_start|>` chat-template
  leakage. Rewrote with `json.JSONDecoder.raw_decode` to pick the first
  well-formed block and a regex that strips chat-template tokens.
- `[processing]` `_dry_run_with_retry` now caps the retry Developer's
  `max_iter` to 3 so it can't wedge on repetition loops.
- `[processing]` `_run_agent_pipeline` cleanup after the state-machine
  refactor: removed orphaned `StateStore` import and dead
  `store = StateStore(redis)` assignment.
- `[processing]` `alfred/agents/crew.py` dead imports removed
  (`RequirementSpec`, `AssessmentResult`, `ArchitectureBlueprint`,
  `Changeset`, `DeploymentResult` were imported but the `output_json` path
  they were meant for is commented out).

### Changed
- `[client]` `PreviewPanel.vue` now renders **full Workflow states and
  transitions tables** (previously just two rows: document_type +
  is_active). Also enriched every other type's detail table: DocType
  (module / naming_rule / submittable flags), Notification
  (days_in_advance / date_changed / value_changed / enabled), Server
  Script (script_type / api_method / event_frequency / cron / disabled),
  Custom Field (default / insert_after / reqd / in_list_view /
  in_standard_filter / description), Client Script (enabled flag).

---

## Phase 3 (2026-04-13)

Intelligence, observability, and refactoring.

### Added

**#15 Think-then-act planning step** `[processing]`
- `crew.py::generate_changeset` and `LITE_TASK_DESCRIPTION` now include a
  "THINK FIRST, ACT SECOND" preamble that forces the Developer to emit a
  numbered 1-6 item PLAN in its first Thought before calling any tool.
  Plan stays in the reasoning channel; Final Answer is raw JSON only.
- Prompt-level change, zero runtime cost, directly addresses the
  retry-loop drift observed in manual QA.

**#13 Reflection minimality** `[processing]`
- New module `alfred/agents/reflection.py`. Post-crew LLM call that reviews
  the changeset against the user's original request and drops items that
  aren't strictly needed.
- Feature-flagged via `ALFRED_REFLECTION_ENABLED=1`; default off.
- Strict JSON parser (`{"remove": [int], "reasons": [str]}`), safety net
  that refuses to strip all items, single-item changesets skipped entirely,
  failures fall through silently (changeset passes through unchanged).
- Emits a `minimality_review` WebSocket event with dropped items + reasons.
- 30 unit tests in `tests/test_reflection.py`.

**#14 Pipeline tracing** `[processing]`
- New module `alfred/obs/tracer.py`. Minimal zero-dep span tracer with
  async context-manager API + parent/child nesting via `ContextVar`.
  No `opentelemetry-api` dependency; call-site API matches OTel so
  swapping is a one-file change.
- JSONL file exporter (thread-locked writes) + optional stderr exporter.
- Enable per-process via `ALFRED_TRACING_ENABLED=1`.
  Configurable path via `ALFRED_TRACE_PATH`, stderr via
  `ALFRED_TRACE_STDOUT=1`.
- Auto-wraps every pipeline phase in a span (see `AgentPipeline.run`).
- 24 unit tests in `tests/test_tracer.py`.

**#12 Pipeline state machine** `[processing]`
- New module `alfred/api/pipeline.py` with `PipelineContext` dataclass and
  `AgentPipeline` orchestrator. 9 named phases: `sanitize`, `load_state`,
  `plan_check`, `enhance`, `clarify`, `resolve_mode`, `build_crew`,
  `run_crew`, `post_crew`.
- Each phase is a method that reads / mutates `self.ctx`, is independently
  unit-testable, and auto-wraps in a tracer span. Adding a new phase is
  two edits (add method + append to `PHASES`) instead of surgery in a
  400-line imperative function.
- `_run_agent_pipeline` in `websocket.py` shrank from ~400 lines to ~10 by
  delegating to the orchestrator.
- Centralized error boundaries: `asyncio.TimeoutError` →
  `PIPELINE_TIMEOUT`, any other exception → `PIPELINE_ERROR`, phase-level
  `ctx.stop()` → emits the stop signal's error after the loop.
- 18 unit tests in `tests/test_pipeline_state_machine.py`.

### Test suite
- 367 passed / 4 skipped (up from 295 at start of Phase 3 - 72 new tests,
  zero regressions).

---

## Phase 2 (2026-04-13)

Context compaction and multi-turn workflows.

### Added

**Handoff summary condenser** `[processing]`
- New module `alfred/agents/condenser.py`. Attaches a `Task.callback` to
  each upstream SDLC task (gather_requirements, assess_feasibility,
  design_solution) that compacts `task_output.raw` in place before the
  next task's context is aggregated.
- Deterministic strategy - no extra LLM call: strip markdown code fences,
  try JSON parse (compact re-emit), find outermost balanced `{...}` or
  `[...]` substring, tail-truncate fallback to 1500 chars.
- `generate_changeset`, `validate_changeset`, `deploy_changeset` are
  skipped so the changeset artifact survives unchanged.
- 26 unit tests in `tests/test_handoff_condenser.py`.

**Conversation memory** `[processing]`
- New module `alfred/state/conversation_memory.py`. Per-conversation
  structured record persisted in Redis under `conv-memory-<conversation_id>`
  via the existing `StateStore.set_task_state`.
- Stores items (op/doctype/name/on, capped at 20), clarifications (q/a,
  capped at 10), recent_prompts (capped at 5, truncated to 200 chars).
- Loaded in `_phase_load_state`, updated in `_phase_clarify` (qa pairs)
  and `_phase_post_crew` (changeset items + prompt), saved before the
  changeset message is sent.
- `render_for_prompt()` produces a text block the enhancer prepends to
  its user message so the LLM resolves "now add a description field to
  that DocType" against the prior turn's items.
- `enhance_prompt` accepts optional `conversation_context` arg.
- `_clarify_requirements` refactored to return `(text, qa_pairs)` so the
  pipeline can capture answers.
- 26 unit tests in `tests/test_conversation_memory.py`.

### Benchmark gate
- Baseline → Phase 1 → Phase 2 cumulative: tokens 76,426 → 57,327
  (-25.0%), wall-clock 247.5s → 223.8s (-9.6%), first-try accuracy held
  at 100%.

---

## Phase 1 + Tool Consolidation (2026-04-13)

MCP tool hardening, framework knowledge graph, and tool consolidation.

### Added

**Framework Knowledge Graph** `[client]`
- New module `alfred_client/mcp/framework_kg.py`. Walks every installed
  bench app's `doctype/*/*.json` at `bench migrate` time and writes
  `alfred_client/data/framework_kg.json` (gitignored).
- In-memory cache with mtime-based invalidation. Re-extracts on file
  change, reuses cached record otherwise.
- Pattern library at `alfred_client/data/customization_patterns.yaml`
  with 5 MVP patterns: `approval_notification`,
  `post_approval_notification`, `validation_server_script`,
  `custom_field_on_existing_doctype`, `audit_log_server_script`. Each has
  `when_to_use` / `when_not_to_use` / `template` / `anti_patterns`.
- 15 unit tests in `test_framework_kg.py`.

**Consolidated tools** `[client] [processing]`
- New MCP tools:
  - `lookup_doctype(name, layer)` - merged framework KG + live site view.
    Replaces `get_doctype_schema` + originally-planned
    `get_framework_doctype` + `list_framework_doctypes`. `layer` is
    `framework` / `site` / `both` (default).
  - `lookup_pattern(query, kind)` - pattern library retrieval. Replaces
    originally-planned `get_customization_pattern` +
    `list_customization_patterns` + `search_framework_knowledge`. `kind`
    is `name` / `search` / `list` / `all` (default).
- Tool count went from 10 (current) + 5 (originally planned) = 15 down to
  12. SWE-Agent ACI principle: fewer richer tools outperform many narrow
  ones.

**MCP tool hardening (per-run state)** `[processing]`
- `init_run_state(mcp_client, conversation_id)` attaches a tracking dict
  to the MCP client at pipeline start. Gated by `ALFRED_PHASE1_DISABLED=1`
  for benchmark opt-out.
- **P2 call budget** - `DEFAULT_CALL_BUDGET = 30` hard cap per conversation.
  Exceeding returns `{"error": "budget_exceeded"}` without dispatching.
- **P4 dedup cache** - identical `(tool_name, args)` calls return the
  cached result without round-tripping.
- **A2 failure counter** - tools that return `{"error": ...}` or raise
  bump a counter; subsequent successful calls surface "Previous failures:
  N" in their `_alfred_notes` so agents can adapt.
- **A3 misuse warning** - calling `dry_run_changeset` before any
  schema-lookup tool adds a warning note.
- 14 unit tests in `tests/test_mcp_tools_runstate.py`.

**Expanded tool docstrings** `[processing]`
- Every `@tool` wrapper in `alfred/tools/mcp_tools.py` got a concrete
  usage example in its docstring (input + expected output shape). Per
  SWE-Agent ACI research, docstring examples add ~15-20 percentage points
  to first-try tool usage correctness for smaller models.

**Pydantic output models via fence-stripping** `[processing]`
- Task callback now strips leading/trailing markdown fences + converts
  Python-dict-repr to JSON before Pydantic validation. Still not wired to
  CrewAI's `output_json` (Ollama produces code fences CrewAI's parser
  can't handle) but the models are the authoritative schema.

**Benchmark harness** `[processing]`
- New `scripts/benchmark_pipeline.py` - runs 6 fixed prompts through
  `_run_agent_pipeline` in-process (fake WebSocket + mock MCP client),
  captures per-run metrics, writes a JSON summary.
- Metrics: `wall_clock_seconds`, `llm_total_tokens`, `llm_completion_count`,
  `mcp_tool_calls`, `mcp_tool_calls_by_name`, `first_try_extraction`,
  `rescue_triggered`, `dry_run_retries`.
- LiteLLM `success_callback` for token accounting, deduped by
  `response.id` to handle streaming chunks.
- Companion `scripts/compare_benchmarks.py` compares two runs and fails
  the gate on: tokens regression > 2%, latency regression > 10%, first-try
  accuracy drop.

**Initial prompt set**:
- `notification_approval_flow` - alert the approver on expense claim
- `custom_field_simple` - priority Select on Sales Order
- `new_doctype_basic` - Training Program DocType
- `notification_different_domain` - notify sales manager on lost opportunity
- `server_script_validation` - validate leave application from_date
- `audit_log` - server script to log changes on Customer

### Changed

**Hardcoded rule purge** `[all]`
- Removed all "Expense Claim" and similar domain-specific examples from
  6 files (`frappe_knowledge.py`, `prompt_enhancer.py`,
  `crew.py::TASK_DESCRIPTIONS`, agent backstories, rescue template,
  clarifier system prompt). Replaced with placeholder phrasing like
  `<target doctype>` / `<link field holding the approver>`. Opinionated
  rules don't belong in prompts that cover hundreds of domains.

**`frappe_knowledge.py` rewrite** `[processing]`
- Trimmed from 165 lines of opinionated guidance to 74 lines of pointers:
  "Use lookup_doctype for schema verification, lookup_pattern for
  idioms". No hardcoded event rules or field assumptions.

### Benchmark gate
- Baseline → Phase 1 (cumulative): tokens 76,426 → 64,140 (-16.1%),
  first-try accuracy held at 100%, wall-clock -4.3%, no regressions.

---

## Manual QA fixes (2026-04-13)

Pre-Phase-1 stabilization from dev.alfred manual testing.

### Fixed
- `[processing]` `getattr(settings, "pipeline_mode", None)` fallback for
  missing attribute after a schema addition.
- `[processing]` Added `_rescue_regenerate_changeset` fallback when
  `_extract_changes` returns empty.
- `[client]` `_reconnect_db_if_stale` detects stale MySQL connections and
  reconnects so zombie connection managers don't wedge after
  `bench restart` killed their DB handle.
- `[client]` `max_lifetime_seconds = 6300` cap in the connection loop so
  stale browser tabs don't occupy worker slots indefinitely.
- `[processing]` Explicit `store.delete_task_state(...)` at the start of
  every new prompt to clear completed CrewState from previous runs.
  Previously a follow-up prompt in the same conversation would load
  completed state and skip every task, erroring with "No task outputs
  available to create crew output".

---

## Pre-Phase-1 foundation (before 2026-04-13)

Not comprehensively changelog'd. Key pieces already in place at Phase 1 start:

- Full 6-agent SDLC crew (`Process.sequential`)
- Lite single-agent pipeline for lower tiers
- MCP client with cross-loop future resolution via `run_coroutine_threadsafe`
- WebSocket handshake with API key + JWT
- Alfred Conversation / Message / Changeset / Audit Log doctypes
- Deployment engine with per-item `frappe.has_permission` re-check
- Dry-run via savepoint + rollback (without the Phase-3 DDL-aware routing)
- Admin portal with Customer / Plan / Subscription / Usage Log / Admin Settings
- Alfred Settings UI with Connection / LLM / Access Control / Limits tabs
- Vue-based chat UI with conversation list + preview panel
- Stale conversation cleanup scheduled job
- Prompt defense sanitizer
- Rate limiting per user per hour
