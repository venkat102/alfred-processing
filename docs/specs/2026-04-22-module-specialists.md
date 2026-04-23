# Module Specialists — Design (V2)

**Date:** 2026-04-22
**Status:** Draft, pending user review
**Supersedes:** none (V1 DocType Builder lives at `docs/specs/2026-04-21-doctype-builder-specialist.md`; this extends the V1 pattern, does not replace it)
**Scope:** `alfred-processing/alfred/` (FastAPI + CrewAI) and `alfred_client/` (Frappe app)
**Architectural plan this implements:** `~/.claude/plans/since-we-are-planning-shiny-hanrahan.md`

## Problem

V1's per-intent Builder specialists know how to *structure* a DocType or Server Script or Workflow, but not how to make one *belong* to a specific ERPNext module. A DocType Builder asked for a Sales Invoice extension produces a structurally valid DocType with System Manager permissions, autoincrement naming, and no awareness that:
- Accounts DocTypes conventionally carry an `Accounts Manager` permission row
- Submittable Accounts documents must post to the General Ledger `on_submit`
- The naming convention for Accounts transactional documents typically includes a fiscal-year segment

The gap is **domain correctness, not structural correctness.** V1 handles structure; V2 handles domain.

Observed today:
- No per-module convention knowledge anywhere in Alfred (`alfred/agents/frappe_knowledge.py`, `alfred/knowledge/fkb.py`, `customization_patterns.yaml` are all framework-level).
- `_ERPNEXT_FIELD_SMELLS` at `alfred/api/pipeline.py:89-100` recognizes ERPNext-typical field names but only as a drift guardrail, never as a directional signal.
- Built-in defaults at `alfred/agents/crew.py:114` hard-code new DocTypes to `module: "Alfred"` — no real module selection happens.

## Goal

Introduce a family of **module specialist agents** (one per ERPNext module) that are invoked twice per build:
1. **Before** the intent specialist emits its changeset, to provide domain context that flows into the prompt.
2. **After** the intent specialist emits, to validate the output against module conventions and surface notes to the user in the preview panel.

Module specialists are **intent-agnostic** — the same `AccountsSpecialist` serves DocType, Server Script, Workflow, Report, and every future intent builder. There is exactly one specialist per module, not one per (intent × module) pair. Agent count at steady state ≈ 9 intents + N modules, no grid.

The V2 flag is `ALFRED_MODULE_SPECIALISTS=1`, independent of V1's `ALFRED_PER_INTENT_BUILDERS=1`. V2 requires V1 on (module specialists hook into the intent specialist's prompt enhancement path).

## V2 scope

- Ship the substrate end-to-end: `ModuleRegistry` loader, module detection phase, pre/post-pass orchestration, validation-note plumbing, preview panel rendering.
- Ship **exactly one pilot module**: `Accounts`. Concrete KB content, real validation rules, real backstory. No other modules ship in V2.
- Wire the pre/post-pass into V1's existing `DocTypeBuilder` specialist. No other intent specialists receive module context in V2 (they ship later as each intent specialist lands).

V2 is the calibration investment. V2.1+ adds HR, Stock, Selling, etc. as separate slice specs.

## Non-goals (V2)

- Module specialists for intents that haven't shipped yet (Report/Dashboard/Workflow builders don't exist yet — their module integration ships with them).
- Cross-module customizations ("Sales Invoice that creates a Project task"). Classifier stays single-module; low-confidence or dual-ranked classifications fall back to no-specialist.
- Runtime-computed module knowledge. The module KB is static JSON at V2; dynamic knowledge (e.g. from site-specific overrides) is a V3+ concern.
- Module specialists calling each other. No cross-module agent chatter.
- Changes to V1's `ALFRED_PER_INTENT_BUILDERS` behaviour when the V2 flag is off.

## Prerequisites

V1 must be merged and working. This spec references:
- `alfred/registry/loader.py::IntentRegistry` (shape to mirror)
- `alfred/registry/intents/_meta_schema.json` (validation pattern)
- `alfred/agents/builders/doctype_builder.py::enhance_generate_changeset_description` (prompt enhancement pattern)
- `alfred/handlers/post_build/backfill_defaults.py::backfill_defaults_raw` (post-processing pattern)
- `alfred/orchestrator.py::classify_intent`, `IntentDecision` (classifier pattern)
- `alfred/api/pipeline.py::_phase_classify_intent`, `PipelineContext.intent` (phase pattern)

## Architecture

**Before (V1):**
```
prompt -> classify_mode -> classify_intent -> build_alfred_crew(intent=...)
       -> Requirement -> Assessment -> Architect -> Developer (V1 specialist) -> Tester -> Deployer
       -> _extract_changes -> backfill_defaults_raw -> WebSocket
```

**After (V2, flag on):**
```
prompt -> classify_mode -> classify_intent -> classify_module (NEW)
       -> provide_module_context (NEW pre-pass) -> stashes module_context on ctx
       -> build_alfred_crew(intent=..., module=..., module_context=...)
              -> same 6-agent crew, intent specialist's description now carries
                 the module KB snippet as a third prompt addendum (after intent
                 registry checklist)
       -> _extract_changes -> backfill_defaults_raw (now also applies module defaults)
       -> validate_module_output (NEW post-pass) -> appends notes to ctx.changes
       -> WebSocket (with module_validation_notes in the changeset payload)
```

The two new pipeline phases (`classify_module`, and the pre/post-pass glue) run as **Python orchestration** around the existing CrewAI crew, not as new CrewAI tasks. Two reasons:
1. Avoids inflating the crew sequence — CrewAI task overhead is non-trivial and the module specialist's two calls are focused enough to not need agent-to-agent handoff.
2. Keeps the module specialist testable in isolation (a single async function call, patchable in unit tests).

The module specialist **is** a CrewAI `Agent` object with a rich backstory and tool scope, but the Agent is invoked via direct `ollama_chat` calls templated from the Agent's backstory + task prompts, not via `Crew.kickoff()`. This is the same approach `_classify_intent_llm` takes in V1's `alfred/orchestrator.py`.

## Components

### A. Module knowledge base (NEW)

Location: `alfred/registry/modules/`.
One JSON file per module. V2 ships `accounts.json` only.

Meta-schema: `alfred/registry/modules/_meta_schema.json` — mirrors the intent meta-schema's self-validating pattern.

Example `alfred/registry/modules/accounts.json`:

```json
{
  "module": "accounts",
  "display_name": "Accounts",
  "frappe_module_key": "Accounts",
  "backstory": "You are the Alfred ERPNext Accounts domain authority. You know: GL posting on_submit of submittable documents; Cost Center / Party Type / Account Head conventions; multi-currency handling and exchange rate sources; Accounts Manager vs Accounts User role separation; period-lock, fiscal-year, and posting-date discipline; and anti-patterns including bypassing GL, skipping party validation, and hardcoding currency.",
  "conventions": {
    "permissions_add_roles": [
      {"role": "Accounts Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
      {"role": "Accounts User", "read": 1, "write": 1, "create": 1, "delete": 0}
    ],
    "naming_patterns": ["format:ACC-.YYYY.-.####"],
    "typical_linked_doctypes": ["Customer", "Supplier", "Item", "Cost Center", "Account"],
    "required_hooks_for_submittable": ["on_submit_gl_posting"],
    "gotchas": [
      "All submittable DocTypes should post to General Ledger via on_submit hook",
      "Multi-currency fields require explicit exchange_rate and validation",
      "Posting date controls GL entry sequencing - never set it from the client",
      "Fiscal year must be derived, not user-provided"
    ]
  },
  "validation_rules": [
    {
      "id": "accounts_submittable_needs_gl",
      "severity": "warning",
      "when": {"doctype": "DocType", "data.is_submittable": 1},
      "message": "Submittable Accounts DocTypes conventionally post GL entries on submit. No on_submit hook detected.",
      "fix": "Add a Server Script with doctype_event='on_submit' and reference_doctype=<this DocType> that inserts a GL Entry."
    },
    {
      "id": "accounts_needs_accounts_manager_perm",
      "severity": "advisory",
      "when": {"doctype": "DocType"},
      "message": "Accounts DocTypes typically include an 'Accounts Manager' permission row.",
      "fix": "Add {role: 'Accounts Manager', read: 1, write: 1, create: 1, delete: 1} to permissions."
    }
  ],
  "detection_hints": {
    "target_doctype_matches": [
      "Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry",
      "GL Entry", "Cost Center", "Account", "Fiscal Year", "Currency Exchange"
    ],
    "keyword_hints": [
      "account", "accounting", "ledger", "gl", "invoice", "journal",
      "payment entry", "cost center", "fiscal", "currency exchange"
    ]
  }
}
```

### B. `ModuleRegistry` loader (NEW)

Location: `alfred/registry/module_loader.py`.
Mirrors `IntentRegistry` exactly: singleton-cached, meta-schema-validated at test time, `.get(module_key)`, `.modules()`, `.for_doctype(doctype)` lookups, plus a new `.detect(prompt, target_doctype)` helper that walks `detection_hints` and returns a `(module_key, confidence)` tuple or `(None, None)`.

### C. Module detection phase (NEW)

Location: `alfred/orchestrator.py` — extend with `detect_module(prompt, target_doctype, site_config) -> ModuleDecision`. Mirrors `classify_intent`:
- Heuristic first: `ModuleRegistry.detect(prompt, target_doctype)` — matches known target DocTypes and keywords.
- LLM fallback: only when heuristics return None or low confidence. Small Ollama call constrained to the list of registered module keys + `"unknown"`.
- Returns `ModuleDecision(module, reason, confidence, source)` dataclass mirroring `IntentDecision`.

Pipeline integration: new phase `classify_module` in `AgentPipeline.PHASES` right after `classify_intent`. Populates `ctx.module`, `ctx.module_confidence`, `ctx.module_source`, `ctx.module_reason`. No-op when:
- Mode != "dev"
- `ALFRED_MODULE_SPECIALISTS` env var != "1"
- `ALFRED_PER_INTENT_BUILDERS` is off (V2 depends on V1)

### D. Module specialist invocation (NEW)

Location: `alfred/agents/specialists/module_specialist.py`.

Two module-level async functions, each wrapping one LLM call templated from the loaded module KB's backstory:

```python
async def provide_context(
    *, module: str, intent: str, target_doctype: str | None,
    site_config: dict,
) -> str:
    """Call the module specialist's context pass. Returns a prompt snippet
    to inject into the intent specialist's task description.

    Returns "" if module is unknown or no KB entry exists.
    """

async def validate_output(
    *, module: str, intent: str, changes: list[dict],
    site_config: dict,
) -> list[ValidationNote]:
    """Call the module specialist's validation pass. Returns a list of
    structured notes (same shape as dry_run_issues + a source tag).

    Returns [] if module is unknown, no KB entry exists, or changes is
    empty.
    """
```

Each function:
1. Loads the module KB via `ModuleRegistry.load().get(module)`.
2. Builds a system prompt from `kb["backstory"]` + task-specific instructions.
3. For `provide_context`: a deterministic short LLM call (temperature 0, max_tokens ~400) that summarizes the KB subset relevant to the given `intent` + `target_doctype`. Response is a plain prompt snippet to append.
4. For `validate_output`: a constrained LLM call that emits JSON matching `ValidationNote` shape. Parsed with the same `_parse_plan_doc_json`-style robustness (fence-stripping, balanced-object fallback) we already have in `alfred/handlers/plan.py`.
5. Also applies KB's `validation_rules[]` deterministically (no LLM) as a baseline check, merging LLM-discovered and rule-discovered notes. LLM catches subtleties; rules catch the obvious. Both paths use the same severity ladder.

Cacheability: `provide_context` is cached per `(module, intent, target_doctype)` tuple with a short TTL (5 min) in Redis, keyed similarly to the existing Frappe KB cache. `validate_output` is not cached (depends on changeset contents).

### E. `ValidationNote` model (NEW)

Location: `alfred/models/agent_outputs.py` — add alongside `ChangesetItem`.

```python
class ValidationNote(BaseModel):
    severity: Literal["advisory", "warning", "blocker"]
    source: str  # e.g. "module_specialist:accounts", "module_rule:accounts_submittable_needs_gl"
    field: Optional[str] = None  # dotted path into the changeset item, e.g. "permissions"
    issue: str
    fix: Optional[str] = None
    changeset_index: Optional[int] = None  # which ctx.changes item this applies to
```

Lives alongside (not inside) `ChangesetItem` because notes can span multiple items or attach to the changeset as a whole.

### F. Pipeline wiring (MODIFIED)

Location: `alfred/api/pipeline.py`.

Add two phases and extend `PipelineContext`:

```python
# PipelineContext additions
module: str | None = None
module_confidence: str | None = None
module_source: str | None = None
module_reason: str | None = None
module_context: str = ""  # snippet from provide_context pre-pass
module_validation_notes: list[dict] = field(default_factory=list)

# PHASES additions
PHASES = [
    ..., "orchestrate", "classify_intent", "classify_module",
    "enhance", "clarify", "inject_kb", "resolve_mode",
    "provide_module_context",  # NEW pre-pass
    "build_crew", "run_crew", "post_crew",
    # module validation happens inside _phase_post_crew, after backfill
]
```

`_phase_classify_module` — mirrors `_phase_classify_intent`; flag-gated; populates `ctx.module*`.

`_phase_provide_module_context` — flag-gated; calls `provide_context(...)` and stashes result on `ctx.module_context`. Passes `ctx.module_context` into `build_alfred_crew(..., module_context=ctx.module_context)`.

Inside `_phase_post_crew`, after `backfill_defaults_raw`, add a call to `validate_output(...)` and append results to `ctx.module_validation_notes`. The WebSocket emission at the end of the phase already serializes `ctx.changes`; we serialize `ctx.module_validation_notes` as a sibling key in the payload.

### G. Intent specialist prompt enhancement (MODIFIED)

Location: `alfred/agents/builders/doctype_builder.py`.

Extend `enhance_generate_changeset_description(base, module_context=None)`:

```python
def enhance_generate_changeset_description(
    base: str, module_context: str = "",
) -> str:
    if _CHECKLIST_MARKER in base:
        # intent checklist already applied; only append module context
        # if it isn't there yet
        if module_context and _MODULE_CONTEXT_MARKER not in base:
            return base + "\n\n" + _wrap_module_context(module_context)
        return base
    schema = IntentRegistry.load().get("create_doctype")
    checklist = render_registry_checklist(schema)
    out = base + "\n\n" + checklist
    if module_context:
        out += "\n\n" + _wrap_module_context(module_context)
    return out
```

Idempotency preserved via `_MODULE_CONTEXT_MARKER` (parallel to `_CHECKLIST_MARKER`). `_wrap_module_context` adds a clear header so the LLM can distinguish the module context block from the intent checklist.

The dispatcher in `alfred/agents/crew.py::_enhance_task_description` gains a `module_context` kwarg and forwards it to the builder when applicable.

### H. Module-aware defaults in backfill (MODIFIED)

Location: `alfred/handlers/post_build/backfill_defaults.py`.

Extend `backfill_defaults_raw(changes, module=None)` to:
1. Apply intent registry defaults (V1 behaviour, unchanged).
2. If `module` is set and `ModuleRegistry` has an entry: apply that module's `conventions.permissions_add_roles` **additively** to the item's `data.permissions` list (deduplicated by role). Record each added row in `field_defaults_meta` with `source: "default"` and rationale `"Added because target module is <module_display_name>."`.
3. If the module declares a `naming_patterns` entry and `autoname` was defaulted by the intent registry (source=="default"), swap in the module's first naming pattern and update the meta's rationale.

Module defaults always layer **on top** of intent defaults; never overwrite user values.

### I. `alfred_client` preview panel (MODIFIED)

Location: `alfred_client/alfred_client/public/js/alfred_chat/PreviewPanel.vue`.

Two changes:
1. Read `changeset.module_validation_notes` from the payload and render them in the existing validation banners area (lines ~115-131 in today's PreviewPanel). Styled by severity — blocker = danger banner, warning = warn, advisory = info. Each note shows `issue` + `fix` + `source`.
2. Add a small "module badge" at the top of the changeset preview showing which module was detected, so users can verify Alfred is reasoning in the right domain.

No new CSS classes beyond the existing `alfred-banner--*` family.

## Data flow

```
1. user -> [client]    "Create a Sales Invoice Custom Field for project code"
2. [client] -> ws ->   [processing]
3. classify_mode -> "dev"
4. classify_intent -> "create_custom_field" (or "create_doctype" if no target matches)
5. classify_module -> "accounts"  (Sales Invoice matches detection hint)
6. provide_module_context ->
     loads accounts.json backstory
     -> Ollama call -> returns snippet: "Accounts Custom Fields typically
        include an Accounts Manager in permissions. Naming: no convention
        needed (Custom Fields don't autoname). Common gotchas: don't add
        currency-affecting fields without exchange_rate handling."
7. build_alfred_crew(intent="create_custom_field", module="accounts",
                    module_context=<snippet>)
     -> Developer specialist's task description now includes:
        (a) generate_changeset base template
        (b) intent registry checklist (V1)
        (c) module context snippet (V2)
     -> crew runs, Developer emits changeset
8. post_crew:
     _extract_changes -> list[dict]
     backfill_defaults_raw(changes, module="accounts")
       -> adds Accounts Manager permission row, flags in field_defaults_meta
     validate_output(module="accounts", intent="create_custom_field",
                     changes=...)
       -> runs deterministic rules from accounts.json
       -> runs LLM validation pass
       -> merges notes, returns list[ValidationNote]
9. ws -> [client]  {changeset: [...], module_validation_notes: [...]}
10. client renders: pills (from V1) + module banner (new) +
    validation-note banners styled by severity (new)
```

## Error handling

1. **Module classifier returns "unknown"** — `provide_module_context` returns ""; `validate_output` is skipped; backfill module-defaults step is skipped. Behaviour equals V1 with only intent specialists active.
2. **Module classifier errors** — caught, logged, fall back to `module="unknown"` with source `"fallback"`. Same downstream behaviour as (1).
3. **`provide_context` LLM call fails** — log warning, set `ctx.module_context = ""`, continue. Intent specialist runs with only V1 behaviour. No user-visible error.
4. **`validate_output` LLM call fails** — log warning, return empty note list. Deterministic-rule notes (from KB's `validation_rules[]`) still apply. User never sees a crash from validation.
5. **`validate_output` LLM returns malformed JSON** — apply the same robust-parsing pattern as `_parse_plan_doc_json`. If still unrecoverable, fall back to rule-only notes.
6. **Module KB file missing or invalid** — `ModuleRegistry.load()` skips files failing meta-schema; loader logs a warning but does not raise. Missing modules behave as (1).
7. **Feature flag `ALFRED_MODULE_SPECIALISTS=0`** — all new phases are no-ops; `module_context=""` flows through; validation is skipped. Identical to V1 output.
8. **V1 flag off (`ALFRED_PER_INTENT_BUILDERS=0`)** — V2 also no-ops (V2 depends on V1's prompt enhancement path existing).

## Testing

### ModuleRegistry tests (`tests/test_module_registry.py`)
- Meta-schema validates as Draft-07.
- Every file in `alfred/registry/modules/*.json` validates against the meta-schema.
- `ModuleRegistry.load()` is singleton-cached; `.get("accounts")` returns the schema; `.for_doctype("Sales Invoice")` returns accounts; `.detect()` finds accounts from prompt + target DocType.

### Module detection tests (`tests/test_detect_module.py`)
- Heuristic match via target DocType: `detect_module("customize Sales Invoice", ...)` -> `module="accounts", source="heuristic"`.
- Heuristic match via keyword: `detect_module("add a GL field somewhere", None, ...)` -> `module="accounts", source="heuristic"`.
- Heuristic miss -> LLM fallback (patched in test).
- LLM failure -> fallback to unknown.
- Ambiguous prompt -> unknown (falls back to V1 behaviour).

### Module specialist tests (`tests/test_module_specialist.py`)
- `provide_context` with stubbed Ollama returns snippet containing KB-derived facts.
- `provide_context` for unknown module returns "".
- `validate_output` applies deterministic rules: submittable DocType without on_submit hook -> warning note.
- `validate_output` merges rule notes and LLM notes, deduplicated by (source, message).
- `validate_output` with empty changes returns [].

### Prompt enhancement extension (`tests/test_doctype_builder.py` — extend)
- `enhance_generate_changeset_description(base, module_context="…")` contains base + intent checklist + module context wrapper.
- Idempotent: calling with the same module_context twice does not double-append.
- `module_context=""` reverts to V1 behaviour (base + intent checklist only).

### Backfill module-defaults tests (`tests/test_backfill_defaults_raw.py` — extend)
- DocType item with `module="accounts"` -> permissions list includes both System Manager (intent default) and Accounts Manager (module default), deduplicated.
- Naming swap: `autoname` defaulted to "autoincrement" (intent) -> swapped to "format:ACC-.YYYY.-.####" (module).
- User-provided permissions -> not overwritten by module rows (layered, not replaced).

### Pipeline integration (`tests/test_pipeline_module_integration.py`)
- PHASES contains `classify_module` and `provide_module_context` in the right order.
- `classify_module` no-ops when V2 flag off.
- `classify_module` populates ctx when flag on and prompt mentions Accounts.
- `_phase_post_crew` appends to `ctx.module_validation_notes` when flag on.
- WebSocket payload contains `module_validation_notes` key.

### Client-side (`alfred_client` frontend)
- Preview panel reads `changeset.module_validation_notes`; renders banners with correct severity class.
- Module badge shows correct display name.
- Flag-off: notes array is empty/absent; banners don't render; no regression for V1-only changesets.

### End-to-end on `dev.alfred`
- `ALFRED_PER_INTENT_BUILDERS=1 ALFRED_MODULE_SPECIALISTS=1`.
- Prompt: "Create a custom field for Sales Invoice called project_code, type Link to Project".
- Expected: module detected as Accounts; changeset emitted with accounts-leaning permissions; no blocker validation notes (custom field is structurally simple); advisory note reminds user about Accounts Manager permission existence.
- Flip V2 flag off: module detection and validation both skip; V1 behaviour unchanged.

## Rollout

1. Ship V2 substrate (loader, detection, specialist, pipeline phases, backfill extension, ValidationNote model) behind `ALFRED_MODULE_SPECIALISTS=1` flag. Merge with flag off.
2. Ship pilot `accounts.json` KB with curated backstory, conventions, ~5 validation rules, ~10 detection hints.
3. Ship client preview changes.
4. Enable flag on `dev.alfred`. Calibrate Accounts KB against real prompts: does validation catch real convention violations? Does context flow change output quality?
5. Once stable for two sprints, flip flag default on. Retire flag-check branches.
6. Follow-on specs: `hr.json` + `HR Specialist`, `stock.json` + `Stock Specialist`, etc. Each is a data PR + a KB validation rule addition. No substrate changes per new module.

## Open questions

- **Validation note rendering priority in the preview panel.** Today the panel shows dry-run issues (line ~119). Module validation notes could live alongside, but: should they be visually distinguishable (different colour / different icon) to avoid confusion with validator-agent notes? Proposal: yes, distinct `alfred-module-note` class. Decide during implementation.
- **Should module-specialist context be visible to the user?** The context snippet fed into the LLM is a reasoning trace Alfred can keep private, or it can be shown as "Alfred consulted Accounts conventions" in the UI. Suggest: private by default, add a "why?" expander later if users ask. Out of scope for V2.
- **Cache TTL for provide_context.** Proposed 5 minutes. If Accounts KB changes, stale caches show old context for 5 minutes. Acceptable for V2 since KB edits require a code deploy anyway. Reconsider if KB becomes site-editable in V3.
- **What happens to `ctx.module_validation_notes` on `blocker` severity?** Proposal: blockers disable the Deploy button in the UI (same mechanism as required-empty fields from V1), but do NOT short-circuit the pipeline. User sees the full changeset + the blocker reason + must edit the prompt to proceed. Decide during implementation.

## Addendum: Family layer (2026-04-23)

This addendum documents the family layer added after the V2 + V3 + canonical-KB-overhaul work shipped. It extends the spec without changing any of the existing behaviour described above.

**Motivation.** The Frappe side (V1) groups 22 intent registries under 4 family builders (`schema_builder`, `reports_builder`, `automation_builder`, `presentation_builder`) with shared base context. The ERPNext side (V2) shipped as a flat list of 13 module KBs - no shared context between related modules. Three problems followed:
1. Facts repeated across several KBs (party + currency + GL posting in accounts + selling + buying; Item + Warehouse in stock + manufacturing + assets) drifted.
2. Cross-module invariants (e.g. "Stock Ledger posts before General Ledger"; "Salary Slip.on_submit does NOT post GL, Payroll Entry does") had no shared home.
3. Frappe intent specialists couldn't consume ERPNext cross-module domain knowledge - the plumbing only surfaced per-module snippets.

**Design.** Add a *family layer above* the 13 module KBs. Families are a labeled context layer that flows through the same plumbing as the module layer, not a restructuring of modules. Four families cover the 12 non-custom modules:

| Family | Member modules | Shared concerns |
|---|---|---|
| `transactions` | accounts, selling, buying | GL posting, party + currency, tax templates, 3-stage SO/DN/SI and PO/PR/PI coupling, Payment Schedule, return_against |
| `operations` | stock, manufacturing, assets | Item identity (serial / batch / stock), Warehouse tree, Bin / SLE append-only, BOM + Routing + Work Order, Asset lifecycle |
| `people` | hr, payroll | Employee state machine, Leave Ledger derivation, Salary Slip vs Payroll Entry GL split, date_of_joining as universal lower bound |
| `engagement` | crm, support, projects, maintenance | Customer-touch lifecycle, SLA / Schedule / Visit cadence, status-field vs docstatus lifecycle, Customer portal role |

`custom` is intentionally familyless - it's the catch-all KB for user-defined DocTypes outside canonical ERPNext modules.

**Schema changes.**
- `modules/_meta_schema.json`: add optional `family` field (enum of the 4 family names). `additionalProperties=false` still holds - every other module field is explicit.
- `modules/_families/_meta_schema.json` (new): schema for family KBs. Required: `name`, `display_name`, `member_modules`, `backstory`, `cross_module_invariants`. Optional: `shared_validation_rules` (not used yet).

**File layout.**
```
alfred/registry/modules/
├── _meta_schema.json                   # module schema + family enum
├── _families/
│   ├── _meta_schema.json               # family schema
│   ├── transactions.json
│   ├── operations.json
│   ├── people.json
│   └── engagement.json
├── accounts.json                       # family: "transactions"
├── selling.json                        # family: "transactions"
├── buying.json                         # family: "transactions"
├── stock.json                          # family: "operations"
├── manufacturing.json                  # family: "operations"
├── assets.json                         # family: "operations"
├── hr.json                             # family: "people"
├── payroll.json                        # family: "people"
├── crm.json                            # family: "engagement"
├── support.json                        # family: "engagement"
├── projects.json                       # family: "engagement"
├── maintenance.json                    # family: "engagement"
└── custom.json                         # no family field (catch-all)
```

**Loader.** `ModuleRegistry.load()` now also globs `_families/*.json` and indexes by name. New APIs:
- `families() -> list[str]` - sorted list of family names.
- `get_family(name) -> dict` - returns the family KB; raises `UnknownFamilyError` on miss.
- `family_for_module(module) -> str | None` - returns the family name for a module; `None` for `custom` or unknown.

Existing APIs (`modules`, `get`, `detect`, `detect_all`, `for_doctype`) unchanged.

**Specialist.** `alfred/agents/specialists/module_specialist.py` gained `provide_family_context(family, intent, site_config, redis=None) -> str`:
- Same Redis + in-memory cache shape as `provide_context`, keyed `alfred:family_ctx:<family>:<intent>` with a **15-minute TTL** (longer than the 5-minute module TTL - families change less).
- Summarises the family KB's `cross_module_invariants` via a triage LLM call using the family backstory as system prompt.
- Returns empty string on unknown family or LLM failure (silent fallback).

The family cache uses a separate in-memory dict (`_family_context_cache`) and Redis namespace from the module cache; calling `provide_context('accounts', ...)` and `provide_family_context('transactions', ...)` are two independent LLM calls that don't cache-hit each other.

**Pipeline.** `_phase_provide_module_context` in `alfred/api/pipeline.py` now assembles a layered string:
- V3 path (`ALFRED_MULTI_MODULE=1`):
  ```
  PRIMARY FAMILY (Transactions):
  <family snippet>

  PRIMARY MODULE (Accounts):
  <module snippet>

  SECONDARY MODULE CONTEXT (Stock):
  <stock snippet>
  ```
- V2 fallback (`ALFRED_MULTI_MODULE=0`): inline prefix `FAMILY CONTEXT (Transactions): <family snippet>\n\n<module snippet>` so single-module callers also see cross-module invariants.
- Familyless modules (`custom`): no family section emitted; existing behaviour preserved.
- Family dedupe: secondary modules in the SAME family as the primary do NOT re-emit a family section - the primary's family header already covers them.

**Frappe builder communication.** All 4 family builders' `_wrap_module_context()` marker now documents the layered sections:
> MODULE CONTEXT (ERPNext domain knowledge - respect these alongside the shape-defining fields above):
> The snippet may contain layered sections labeled PRIMARY FAMILY (cross-module invariants shared across a family like Transactions or Operations), PRIMARY MODULE (the specific ERPNext module's conventions), and SECONDARY MODULE CONTEXT (advisory context from related modules). Treat every labeled section as authoritative. If a FAMILY-level invariant conflicts with a shape-defining default above, the FAMILY invariant wins - families encode real controller-enforced rules.

This is how Frappe intent specialists "communicate" with ERPNext module specialists: labeled sections both sides trust, plus an explicit precedence rule. No CrewAI wiring or tool changes.

**Tests.**
- `tests/test_family_registry.py` (new, 9 tests): family schema self-check, family JSON validation, registry load, grouping assertions, `get_family` / `family_for_module` / member-modules consistency.
- `tests/test_module_specialist_llm.py` (extended, +4 tests): `provide_family_context` cache shape, key structure, unknown-family handling, cache independence from module cache.
- `tests/test_pipeline_multi_module.py` (extended, +3 tests): PRIMARY FAMILY -> PRIMARY MODULE -> SECONDARY MODULE ordering, familyless `custom` skips family section, V2-fallback FAMILY CONTEXT prefix.
- Net: 169 module/family/pipeline/backfill tests + 91 family builder tests all pass.

**Feature-flag story.** No new flags. Families are active whenever `ALFRED_MODULE_SPECIALISTS=1`. The layered section format only kicks in when `ALFRED_MULTI_MODULE=1`; V2-only runs get the inline FAMILY CONTEXT prefix for the primary module's family. Flag-off (`ALFRED_MODULE_SPECIALISTS=0`): specialist is never called, no families loaded, no regression.

**Rollout.** Shipped atomically across 4 commits:
1. Schema + 4 family KBs.
2. `family` field added to 12 module JSONs + loader extension.
3. `provide_family_context` + pipeline layering.
4. Frappe builder backstory updates + tests.
