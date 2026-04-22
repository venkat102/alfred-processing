# DocType Builder Specialist — Design

**Date:** 2026-04-21
**Status:** Draft, pending user review
**Supersedes:** `~/Desktop/.claude/projects/-Users-venkatesh-bench-develop-frappe-bench-apps/docs/superpowers/specs/2026-04-21-alfred-inline-plan-defaults-design.md` (was written against a misread of Alfred's architecture — targeted Plan mode instead of Dev mode)
**Scope:** `alfred-processing/alfred/` (FastAPI + CrewAI) and `alfred_client/` (Frappe app)

## Problem

Alfred's Dev mode produces changesets that are under-specified for user review. When a user says *"Create a DocType called Book with title, author, ISBN"*, the **Developer agent** (the 4th of 6 agents in `alfred/agents/crew.py`) emits a `ChangesetItem` whose `data` dict contains only the fields the user mentioned — and silently accepts Frappe's defaults for everything else. The user never sees choices they'd want to control: `module`, `autoname`, `is_submittable`, `istable`, `issingle`, `permissions`.

Observed failures (2026-04-21):
- Dev mode produced a DocType changeset with `data` missing `module`; Frappe rejected execution with `[DocType, Book]: module`.
- Subsequent retries created the DocType but with no explicit naming rule or permission rows — a working DocType, but shape-defining choices were made silently.

Root cause: Alfred's single generic **Developer agent** handles every customization type (DocType, Server Script, Client Script, Workflow, Print Format, Report, Dashboard, Permission, Role). Each of those domains has deep, distinct quirks — hook events, naming formats, state machines, Jinja templates — and a generalist prompt cannot carry that depth. The result is shallow outputs for every type.

## Goal

Dev mode gains **per-intent Builder specialists** at the final builder stage only (the Developer role). An intent classifier tags each dev-mode prompt with a specific intent (`create_doctype`, `create_server_script`, ...); the crew builder swaps in the matching specialist Developer. Each specialist owns a per-intent **field registry** that declares shape-defining fields with defaults and rationales. The emitted `ChangesetItem` carries every shape-defining field populated (user-provided or defaulted) and a parallel `field_defaults_meta` block describing which fields were defaulted and why. The `alfred_client` changeset review UI renders defaulted fields with visible "default" pills and editable inputs so the user can override any default before deploying.

## V1 scope

**Ship per-intent specialists for `create_doctype` only.** Unknown or other intents fall back to today's generic Developer agent with no behavior change. Subsequent intents (Server Script, Client Script, Workflow, Print Format, Report, Dashboard, Permission, Role) ship as follow-on specs — one per sprint — using the same pattern.

## Non-goals (V1)

- Specialists at roles other than Developer. Requirement Analyst, Feasibility Assessor, Solution Architect, QA Validator, Deployment Specialist stay generalist.
- Multi-intent prompts ("Create a Book DocType and an approval workflow"). Classifier returns a single intent or `unknown`; unknowns fall back to the generic Developer.
- Tool-scope narrowing per specialist. The DocType specialist uses the same MCP tool set as today's Developer (`lookup_doctype`, `lookup_pattern`, `lookup_frappe_knowledge`, `get_site_customization_detail`). Tool narrowing is a future optimization.
- Replacing Alfred's mode classifier. Intent classification is a NEW phase downstream of mode classification; mode classifier (chat / insights / plan / dev) is untouched.
- Rewriting the Plan mode narrative flow. Plan mode continues to produce `PlanDoc` (narrative design docs) unchanged.

## Architecture

**Before — today:**

```
prompt
  -> mode classifier (chat | insights | plan | dev)
  -> dev mode:
       [crew.py build_alfred_crew] builds sequential crew:
         Requirement Analyst
           -> Feasibility Assessor
           -> Solution Architect
           -> Developer (generic, handles all intents)
           -> QA Validator
           -> Deployment Specialist
       -> Changeset = list of {operation, doctype, data: dict}
  -> client changeset review UI
  -> deploy
```

**After — V1:**

```
prompt
  -> mode classifier (unchanged)
  -> dev mode:
       [NEW] intent classifier -> intent in {create_doctype, unknown, ...}
       [crew.py build_alfred_crew(intent=...)]:
         Requirement Analyst         (generalist)
           -> Feasibility Assessor   (generalist)
           -> Solution Architect     (generalist)
           -> Developer specialist   (SWAPPED based on intent)
                 - create_doctype  -> DocTypeBuilder
                 - unknown         -> generic Developer (today's)
           -> QA Validator           (generalist)
           -> Deployment Specialist  (generalist)
       -> Changeset (extended) = list of {operation, doctype, data, field_defaults_meta}
       -> [NEW] defaults backfill post-processor: if specialist forgot a registry field,
          fill it from the registry default and record in field_defaults_meta
  -> client changeset review UI (MODIFIED to render defaults as editable pills)
  -> deploy
```

## Components

### A. Intent schema registry (NEW)

Location: `alfred/registry/intents/`.

One JSON file per intent. V1 ships `create_doctype.json` only. Each file declares the intent key and the list of shape-defining fields the Builder specialist must populate, with defaults and rationales.

`alfred/registry/intents/create_doctype.json`:

```json
{
  "intent": "create_doctype",
  "display_name": "Create DocType",
  "doctype": "DocType",
  "fields": [
    {"key": "module",         "label": "Module",       "type": "link",   "link_doctype": "Module Def", "required": true},
    {"key": "is_submittable", "label": "Submittable?", "type": "check",  "default": 0, "rationale": "Most DocTypes are not submittable. Enable only for documents with a draft / submitted / cancelled lifecycle."},
    {"key": "autoname",       "label": "Naming rule",  "type": "select", "options": ["autoincrement", "field:title", "format:PREFIX-.####", "prompt", "hash"], "default": "autoincrement", "rationale": "Autoincrement is safe when naming intent is unclear. Change if users should see meaningful IDs."},
    {"key": "istable",        "label": "Child table?", "type": "check",  "default": 0, "rationale": "Child tables only exist inside a parent DocType. Enable only for repeating rows."},
    {"key": "issingle",       "label": "Singleton?",   "type": "check",  "default": 0, "rationale": "Single DocTypes store exactly one record (e.g. settings). Enable for config-style documents."},
    {"key": "permissions",    "label": "Permissions",  "type": "table",  "default": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}], "rationale": "System Manager full access is the minimum usable default. Add role rows for end users."}
  ]
}
```

A meta-schema (`alfred/registry/intents/_meta_schema.json`) validates every registry file at test time.

### B. Intent classifier phase (NEW)

Location: `alfred/orchestrator.py` — extend with `classify_intent()` alongside the existing `classify_mode()`.

Runs only when `mode == "dev"`. A lightweight heuristic matcher handles obvious cases ("create doctype", "new doctype"); on no match it calls Ollama with a small classification prompt (temperature 0, 50 tokens max, constrained to the supported intent list + `unknown`).

Returns an `IntentDecision` pydantic model mirroring `ModeDecision`: `{intent: str, confidence: float, source: "heuristic" | "classifier" | "fallback", reason: str}`.

Result is stored in run state and consumed by `build_alfred_crew`.

### C. DocType Builder specialist (NEW)

Location: `alfred/agents/builders/doctype_builder.py`.

Module exposes:
- `build_doctype_builder_agent(site_config, custom_tools)` — returns a CrewAI `Agent` with a DocType-specialized backstory.
- `build_doctype_builder_task(agent, registry_schema)` — returns a CrewAI `Task` whose description is today's `generate_changeset` task prompt + an appended "DocType shape-defining fields" section rendered from the registry JSON.

Backstory addendum (beyond today's Developer backstory):

> "You specialise in creating Frappe DocTypes. You know the distinction between submittable documents (draft / submitted / cancelled lifecycle) and non-submittable documents; between autoincrement, field-based naming, format strings with series, prompt, and hash naming; between parent DocTypes, child tables, and singletons; and the minimum permission set required for a usable DocType. Every DocType you emit MUST include `module`, `is_submittable`, `autoname`, `istable`, `issingle`, and at least one `permissions` row in its `data`. If the user did not specify a value, use the registry default and record which fields were defaulted."

Task description addendum:

> "Shape-defining fields for create_doctype (you MUST include every one of these in `data`):
>   - module                (required, user-provided; if missing, leave as empty string — post-processor will surface)
>   - is_submittable        (default 0)
>   - autoname              (default 'autoincrement')
>   - istable               (default 0)
>   - issingle              (default 0)
>   - permissions           (default [{role: 'System Manager', read: 1, write: 1, create: 1, delete: 1}])
>
> Additionally, emit a parallel `field_defaults_meta` dict on the changeset item describing which of these fields were filled from defaults vs. user-provided, with rationales. Example:
>   \"field_defaults_meta\": {
>     \"is_submittable\": {\"source\": \"default\", \"rationale\": \"...\"},
>     \"permissions\":    {\"source\": \"default\", \"rationale\": \"...\"}
>   }"

### D. ChangesetItem model extension (MODIFIED)

File: `alfred/models/agent_outputs.py`.

Add optional `field_defaults_meta: dict[str, FieldMeta] | None = None` to `ChangesetItem`. Define:

```python
class FieldMeta(BaseModel):
    source: Literal["user", "default"]
    rationale: str | None = None
```

The existing `data: dict[str, Any]` shape is unchanged — `field_defaults_meta` is a parallel, optional annotation consumed by the client review UI. Server-side Frappe deploy ignores it. Old clients continue to work (field is optional).

### E. Specialist dispatch in `build_alfred_crew` (MODIFIED)

File: `alfred/agents/crew.py`.

Signature change: `build_alfred_crew(..., intent: str | None = None)`. When `intent == "create_doctype"`, the Developer agent and its `generate_changeset` task are replaced by the DocType Builder variants. For any other `intent` (including `None` and `"unknown"`), today's generic Developer + generic task are used — preserving current behavior.

Feature flag: the dispatch is only active when `ALFRED_PER_INTENT_BUILDERS=1` in the processing env. When the flag is off, `intent` is ignored and today's generic Developer always runs. This lets V1 ship dark and be enabled per-environment.

### F. Defaults backfill post-processor (NEW)

Location: `alfred/handlers/post_build/backfill_defaults.py`.

Runs on the raw Changeset emitted by the crew, before the client receives it. For each `ChangesetItem` whose `doctype` matches a registry intent, the post-processor:

1. Loads the matching intent schema.
2. For each registry field: if absent from `item.data`, inserts the default value and records `field_defaults_meta[key] = {source: "default", rationale: <registry rationale>}`.
3. For each registry field already present in `item.data`: if not yet in `field_defaults_meta`, records `{source: "user"}` with no rationale.

This is a belt-and-suspenders layer: if a specialist's LLM output drops a field, the user still sees it in the review UI with a visible default rather than a silent omission.

### G. `alfred_client` changeset review UI (MODIFIED)

Location: the Vue / Desk component that renders the changeset for user approval before deploy. (Exact file path — one of the Vue components in `alfred_client/alfred_client/public/` — to be located during implementation.)

Changes:
- Render every key in `item.data` as an editable row. The input type comes from the registry if `item.doctype` matches a registry intent; otherwise a plain text input.
- If `field_defaults_meta[key].source == "default"`, display a "default" pill with the `rationale` as a hover tooltip.
- Editing a row flips `field_defaults_meta[key].source` from `"default"` to `"user"` in local state; the pill disappears.
- Required-empty fields (e.g. `module` with empty value) get a red outline; the Deploy button is disabled while any required-empty field remains.
- The edited `data` + `field_defaults_meta` is sent back when the user clicks Deploy.

## Data flow

```
1. user          -> [alfred_client]   "Create a DocType called Book with title, author, ISBN"
2. [alfred_client] -> WebSocket       prompt
3. [alfred-processing] classify_mode  -> "dev"
4. [alfred-processing] classify_intent -> "create_doctype" (NEW)
5. [alfred-processing] build_alfred_crew(intent="create_doctype")
     -> Requirement / Assessment / Architect run (unchanged)
     -> DocType Builder specialist runs generate_changeset task
     -> QA Validator / Deployment Specialist run (unchanged)
6. [alfred-processing] backfill_defaults post-processor fills any missing registry fields
7. [alfred-processing] -> WebSocket  Changeset with field_defaults_meta
8. [alfred_client] renders changeset review UI with default pills + editable fields
9. user edits any defaults, clicks Deploy
10. [alfred_client] -> WebSocket       edited Changeset
11. [alfred-processing] deploys via Frappe; returns per-item result
12. [alfred_client] renders per-item pass/fail
```

## Error handling

**1. Intent classifier returns `unknown`.** `build_alfred_crew` uses the generic Developer (today's behavior). No specialist path. The changeset review UI still runs but without default pills (no `field_defaults_meta` for non-registry doctypes).

**2. Intent classifier LLM call fails.** Classifier falls back to `unknown` with `source: "fallback"`. Generic Developer runs. Logged as a warning.

**3. DocType Builder specialist produces malformed output (JSON parse fail, missing required fields).** Today's `_extract_changes()` error path in `pipeline.py` is preserved. If extraction succeeds but registry fields are missing, the post-processor backfills — the user sees defaulted values in the review UI and can edit them before deploying.

**4. Post-processor cannot find intent registry (new intent named in a ChangesetItem but no registry file exists).** The item is passed through unchanged with `field_defaults_meta: None`. Logged as info.

**5. Feature flag `ALFRED_PER_INTENT_BUILDERS` disabled.** Intent classifier does not run; generic Developer runs; post-processor does not run. End-to-end behavior matches today.

## Testing

### Registry tests

- Meta-schema validates itself as Draft-07.
- Every file in `alfred/registry/intents/*.json` (except `_meta_schema.json`) validates against the meta-schema.
- `create_doctype.json` contains the six expected field keys.

### Intent classifier tests

- Heuristic match: "create a DocType called X" -> `intent: "create_doctype"`, `source: "heuristic"`, `confidence >= 0.9`.
- Heuristic miss -> LLM call. Use a stub LLM in tests that returns `"create_doctype"` or `"unknown"` based on input.
- LLM failure -> returns `unknown` with `source: "fallback"`.

### DocType Builder tests

- `build_doctype_builder_agent` returns a CrewAI Agent with the extended backstory.
- `build_doctype_builder_task` renders the registry schema into the task description string.
- Output of a simulated Builder run on "Create DocType Book with title" contains `module`, `is_submittable`, `autoname`, `istable`, `issingle`, `permissions` in `data`, plus `field_defaults_meta` for each defaulted field.

### Backfill post-processor tests

- Item whose `doctype == "DocType"` and `data` missing `autoname` -> post-processor fills `autoname = "autoincrement"` and records `field_defaults_meta["autoname"]`.
- Item whose `data` already has `autoname = "field:title"` -> post-processor records `field_defaults_meta["autoname"] = {source: "user"}` and does not overwrite.
- Item for a doctype with no matching registry (e.g. `"Custom Field"` before that specialist ships) -> passed through untouched, `field_defaults_meta: None`.

### Integration test (end-to-end through dev pipeline)

- Stub mode+intent classifiers to return `dev` + `create_doctype`. Stub crew result to a minimal Developer output with only user-provided fields. Assert: the final changeset passed to the WebSocket contains all six registry fields and a populated `field_defaults_meta`.

### Feature-flag regression test

- With `ALFRED_PER_INTENT_BUILDERS=0`, run a dev-mode prompt. Assert: no intent classifier call, no post-processor call, changeset shape is exactly today's (no `field_defaults_meta`).

### Frontend tests (alfred_client)

- Changeset with `field_defaults_meta["autoname"].source == "default"` renders a "default" pill with the registry rationale as tooltip.
- Editing the pill's input flips the local state's `source` to `"user"` and hides the pill.
- Required-empty field (e.g. `module: ""`) disables Deploy.

### End-to-end manual test on `dev.alfred`

- Enable `ALFRED_PER_INTENT_BUILDERS=1`. Ask Alfred "Create a DocType called Book with title, author, and ISBN fields".
- Expected: changeset review UI shows the six shape-defining fields with pills on `is_submittable`, `autoname`, `istable`, `issingle`, `permissions`; `module` is red-outlined with Deploy disabled. Fill `module` -> Deploy enables.
- Deploy -> DocType Book exists in Frappe with `autoname = "autoincrement"`, `is_submittable = 0`, System Manager permission row.

## Rollout

1. Ship V1 behind `ALFRED_PER_INTENT_BUILDERS` flag (default off). Merge to main.
2. Enable on `dev.alfred` first. Run the end-to-end manual test.
3. If stable for one sprint, flip default on. Retire the flag-check branches two sprints later.
4. Add follow-on specialists one intent at a time, each with its own registry JSON and Builder module under `alfred/agents/builders/`. No changes to the dispatcher, post-processor, or client UI are needed per new intent — only a registry file and a builder module.

## Follow-on specialists (not V1)

Each ships as a separate spec following the V1 template:
- `create_server_script` — hook events, target doctype, sync/async
- `create_client_script` — form/list/report view, enabled, DocType
- `create_workflow` — states, transitions, per-state roles, email alerts
- `create_print_format` — Jinja template, page dimensions, letterhead
- `create_report` — query/script/builder variant, columns, filters
- `create_dashboard` — charts, number cards, shortcuts
- `create_permission` — role perm, user perm, permlevel, if_owner
- `create_role` — role profile, module profile, desk access

## Open questions

- Where exactly does the client changeset review UI live today? (A Vue component inside `alfred_client/`.) The modification in Component G depends on locating it during implementation. Not a blocker for spec approval.
- Should the intent classifier also run for Plan mode so PlanDoc summaries mention which builder will produce the final changeset? Out of scope for V1.
- Should `field_defaults_meta` be persisted to conversation memory so a refine-flow can reference earlier default choices? Out of scope for V1.
