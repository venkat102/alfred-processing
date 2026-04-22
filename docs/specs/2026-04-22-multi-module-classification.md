# Multi-Module Classification (V3) — Design

**Date:** 2026-04-22
**Status:** Draft, pending user review
**Supersedes:** none (extends V2)
**V2 spec:** `docs/specs/2026-04-22-module-specialists.md`
**Scope:** `alfred-processing/alfred/` and `alfred_client/`

## Problem

V2 ships single-module classification: `ctx.module: str | None`. One detected module → one specialist → one provide_context + one validate_output call.

Cross-module prompts are real and V2 handles them poorly:

- *"Create a Sales Invoice that auto-creates a Project task on submit"* spans **Accounts** and **Projects**. V2's classifier picks one (whichever module's hint wins the race), runs only that specialist, and the other module's conventions are silently ignored.
- *"Add a custom field on Employee that feeds into Salary Slip calculation"* spans **HR** and **Payroll**. V2 picks one.
- *"Link Asset to a Cost Center for depreciation reporting"* spans **Assets** and **Accounts**. V2 picks one.

When V2 picks the wrong module, the specialist produces Accounts-flavoured output for an HR prompt — confidently wrong is worse than no specialist at all. When V2 picks correctly but the prompt is genuinely multi-module, half the domain context is missing.

The V2 spec called this out as V3+ scope: "Cross-module requests ... are out of scope for V2; multi-module orchestration is V3+."

## Goal

Add multi-module classification that detects **primary + secondary modules** for a single prompt, invokes the primary specialist fully (context + validation) and secondary specialists in a reduced mode (context only, validation observational), and merges their outputs deterministically. The user sees the primary module in the changeset preview's badge, with secondary modules listed as contributing context; validation notes are tagged by their source module.

V3 must not regress V2. When the classifier detects exactly one module (the common case), behaviour is identical to V2 today.

## V3 scope

- **Two-tier classification** — primary module + secondary modules. Secondary modules contribute context but do not gate deploy on blockers.
- **Specialist invocation fan-out** — the new pipeline phase calls `provide_context` for each detected module in parallel (bounded); the validation pass similarly.
- **Deterministic merge rules** — concrete policies for permission rows, naming patterns, validation note severity, and backstory concatenation. No LLM arbitration.
- **Pilot coverage** — V3 ships with classifier tuning for the three common cross-module pairs Alfred users hit today: Accounts+Projects, HR+Payroll, Assets+Accounts. Others surface organically as keywords/targets align.

## Non-goals

- **Unbounded module fan-out.** Detection caps at **1 primary + up to 2 secondaries** (3 modules total). More than that is treated as a signal the prompt needs to be split by the user, not by the classifier.
- **Cross-module conflict arbitration by LLM.** All merge policies are deterministic; no agent deliberation.
- **Multi-primary ambiguity resolution.** If two modules tie for primary, classifier breaks the tie deterministically (alphabetical on module key as last resort, logged as low confidence).
- **Module specialists calling each other.** V2's one-way invocation stays. No cross-chatter.
- **Retroactive changes to V2's single-module tests.** V3 behaviour is additive and flag-gated.

## Prerequisites

V2 must be on. V3 introduces a third flag, `ALFRED_MULTI_MODULE=1`, gated additionally on `ALFRED_PER_INTENT_BUILDERS=1` and `ALFRED_MODULE_SPECIALISTS=1`. When the V3 flag is off, behaviour is exactly V2.

## Architecture

**V2 today:**
```
classify_module -> ctx.module = "accounts"
provide_module_context -> ctx.module_context = "<accounts snippet>"
build_crew -> specialist's prompt includes accounts snippet
post_crew -> backfill(module="accounts") + validate_output(module="accounts")
```

**V3 with flag on:**
```
classify_modules (NEW phase) -> ctx.module = "accounts" (primary)
                                ctx.secondary_modules = ["projects"]
                                ctx.module_confidence = "high"
provide_module_contexts (REPLACES V2's phase):
    - primary: provide_context(accounts, intent, target_doctype) -> snippet_p
    - secondary: provide_context(projects, intent, target_doctype) -> snippet_s
    - merged: ctx.module_context = join([snippet_p, snippet_s])
              ctx.module_secondary_contexts = {"projects": snippet_s}
build_crew -> specialist's prompt includes concatenated context with clear
              PRIMARY / CONTEXT-FROM headers
post_crew:
    backfill: applies primary module's permission_add_roles +
              UNION of secondary modules' permission_add_roles,
              deduped by role name. Primary's naming_patterns wins.
    validate_output:
        - primary module: all notes included verbatim, blockers gate deploy
        - secondary modules: notes included but severity CAPPED at "warning"
                             (a secondary module cannot gate deploy)
    ctx.module_validation_notes = [primary notes] + [secondary notes]
    emit payload with detected_module=primary, detected_module_secondaries=[...]
```

The V3 flag gate happens in `classify_modules`: when off, only a single module is classified (V2 behaviour preserved), and `ctx.secondary_modules = []`.

## Components

### A. `ModuleRegistry.detect_all()` (NEW)

File: `alfred/registry/module_loader.py`.

Add alongside the existing `detect()` method:

```python
def detect_all(
    self, *, prompt: str, target_doctype: str | None, max_secondaries: int = 2,
) -> tuple[str | None, str, list[str]]:
    """Return (primary_module, confidence, secondary_modules).

    Finds the strongest match (primary), then scans remaining modules
    for additional hits and returns up to max_secondaries more. Primary
    is chosen by:
      1. target_doctype match (confidence="high")
      2. then keyword match (confidence="medium")
    Secondaries use only the keyword path and never exceed the primary's
    confidence level.

    Returns (None, "", []) when no module matches - same shape as detect().
    """
```

Semantics:
- target_doctype match always wins primary (one module owns a DocType).
- Keyword matches past the primary become secondaries, deduped and capped.
- Ordering of secondaries: by keyword-match count, then alphabetical for ties.

### B. `detect_modules` in orchestrator (NEW / alongside existing)

File: `alfred/orchestrator.py`.

Add `detect_modules(prompt, target_doctype, site_config) -> ModulesDecision`:

```python
@dataclass
class ModulesDecision:
    module: str | None           # primary
    secondary_modules: list[str] # 0..max_secondaries (default 2)
    reason: str
    confidence: str              # "high" | "medium" | "low"
    source: str                  # "heuristic" | "classifier" | "fallback"
```

Heuristic path uses `ModuleRegistry.detect_all()`. LLM fallback remains single-primary only (no multi-module inference from the LLM in V3 — too costly for too little signal). When heuristic returns a primary without secondaries, LLM fallback is the existing V2 path.

The existing `detect_module()` function stays intact for V2 compatibility but internally delegates to `detect_modules()` when called.

### C. `PipelineContext` additions (MODIFIED)

File: `alfred/api/pipeline.py`.

Extend alongside V2's module fields:

```python
# V3 multi-module additions.
secondary_modules: list[str] = field(default_factory=list)
module_secondary_contexts: dict[str, str] = field(default_factory=dict)
```

Primary module stays in `ctx.module`. Validation notes already carry a `source` tag (module_rule:<id> or module_specialist:<module>), so no new field there — the client groups by source.

### D. Pipeline phase changes (MODIFIED)

File: `alfred/api/pipeline.py`.

`_phase_classify_module` becomes `_phase_classify_modules` (kept at the same position in `PHASES`):

```python
async def _phase_classify_modules(self):
    # ... same flag gating as V2 ...
    if os.environ.get("ALFRED_MULTI_MODULE") == "1":
        decision = await detect_modules(...)
        ctx.module = decision.module
        ctx.secondary_modules = decision.secondary_modules
    else:
        # V2 compat path
        decision = await detect_module(...)
        ctx.module = decision.module
        ctx.secondary_modules = []
    # ... shared populate of confidence/source/reason ...
```

`_phase_provide_module_context` becomes `_phase_provide_module_contexts`:

```python
async def _phase_provide_module_contexts(self):
    # primary first
    primary_ctx = await provide_context(module=ctx.module, ..., redis=redis)
    secondary_ctxs = {}
    for m in ctx.secondary_modules:
        s = await provide_context(module=m, ..., redis=redis)
        if s:
            secondary_ctxs[m] = s
    # Merge into a single prompt snippet with clear headers
    parts = [f"PRIMARY MODULE ({kb_display_name(ctx.module)}):\n{primary_ctx}"]
    for m, s in secondary_ctxs.items():
        parts.append(f"SECONDARY MODULE CONTEXT ({kb_display_name(m)}):\n{s}")
    ctx.module_context = "\n\n".join(parts)
    ctx.module_secondary_contexts = secondary_ctxs
```

`_phase_post_crew` validation call:

```python
# Primary module: full severity, gates deploy
primary_notes = await validate_output(module=ctx.module, ...)
# Secondary modules: severity capped at warning
secondary_notes: list[ValidationNote] = []
for m in ctx.secondary_modules:
    notes = await validate_output(module=m, ...)
    for n in notes:
        capped = ValidationNote(
            severity="warning" if n.severity == "blocker" else n.severity,
            source=n.source, issue=n.issue, field=n.field, fix=n.fix,
            changeset_index=n.changeset_index,
        )
        secondary_notes.append(capped)
ctx.module_validation_notes = [n.model_dump() for n in primary_notes + secondary_notes]
```

### E. Backfill merge policy (MODIFIED)

File: `alfred/handlers/post_build/backfill_defaults.py`.

`backfill_defaults_raw(changes, *, module=None, secondary_modules: list[str] | None = None)`:

Merge order:
1. Intent defaults (V1).
2. Primary module's `permissions_add_roles`, deduped by role.
3. Each secondary module's `permissions_add_roles`, deduped by role (skipping roles already present from steps 1 or 2).
4. Naming pattern: **primary only** — secondary modules never override naming.
5. `field_defaults_meta.permissions.rationale` becomes multi-sentence: primary first, then each secondary's contribution.

Unknown module keys in `secondary_modules` are skipped silently (logged as info) — unknown modules don't error the pipeline.

### F. WebSocket payload extension (MODIFIED)

File: `alfred/api/pipeline.py` — the existing `type: "changeset"` send.

Add two keys alongside `detected_module`:

```python
"detected_module": ctx.module,                    # primary (V2)
"detected_module_secondaries": ctx.secondary_modules,  # V3 additions
"module_confidence": ctx.module_confidence,       # surfaced for UI badge colour
```

### G. `alfred_client` preview panel (MODIFIED)

File: `alfred_client/alfred_client/public/js/alfred_chat/PreviewPanel.vue`.

Two changes:
1. **Module badge** — when `detected_module_secondaries.length > 0`, badge shows `"Module context: Accounts (with Projects)"` instead of just `"Module context: Accounts"`.
2. **Validation notes list** — notes already tag source via the `source` field (`module_rule:<rule_id>` / `module_specialist:<module>`). Client groups notes by source module, displayed as subheaders: `"Accounts: …"`, `"Projects: (advisory only) …"`. Secondary-module notes get a visual marker (parenthetical "(advisory only)") since they can't gate deploy.

### H. Agent-prompt concatenation (MODIFIED)

File: `alfred/agents/builders/doctype_builder.py`.

`enhance_generate_changeset_description` already accepts `module_context` as a string. V3 doesn't change the signature — the pipeline concatenates primary + secondary snippets into a single string with clear headers (see Component D). The builder stays intent-specific, module-axis-agnostic.

## Data flow example

User prompt: *"Create a Sales Invoice that auto-creates a Project task on submit"*

```
1. classify_mode -> "dev"
2. classify_intent -> "create_doctype" (V1)
3. classify_modules (V3):
     detect_all(prompt, target_doctype="Sales Invoice")
       -> primary: accounts (target_doctype match, "high")
       -> secondaries: ["projects"] (keyword match on "project task")
     ctx.module = "accounts"
     ctx.secondary_modules = ["projects"]
4. provide_module_contexts:
     accounts snippet: "Accounts conventions: GL posting on submit, ..."
     projects snippet: "Projects conventions: tasks must link to a Project, ..."
     ctx.module_context =
       "PRIMARY MODULE (Accounts):\n...\n\n"
       "SECONDARY MODULE CONTEXT (Projects):\n..."
5. build_crew -> DocType specialist's prompt includes the merged context.
6. run_crew -> Developer emits changeset.
7. post_crew:
     backfill: intent defaults + accounts roles (Accounts Mgr, Accounts User)
               + projects roles (Projects Mgr, Projects User) - deduped.
               autoname: accounts's "ACC-.YYYY.-.#####" (primary wins).
     validate_output(accounts): 2 notes (full severity).
     validate_output(projects): 1 note; if severity=="blocker", capped to "warning".
     ctx.module_validation_notes = [both lists merged].
8. WebSocket emit:
     detected_module = "accounts"
     detected_module_secondaries = ["projects"]
     module_validation_notes = [tagged by source]
9. Client renders:
     Badge: "Module context: Accounts (with Projects)"
     Notes grouped: "Accounts: ..." and "Projects: (advisory only) ..."
```

## Error handling

1. **No modules detected.** Same as V2: `ctx.module = None`, no specialists invoked, no validation notes.
2. **One module detected (V2-equivalent).** `ctx.secondary_modules = []`, all downstream paths match V2 behaviour exactly.
3. **Primary + secondary detected, one secondary's LLM context call fails.** `secondary_ctxs[that_module]` is simply absent; primary snippet still flows. Logged warning.
4. **Primary validation call fails.** Same as V2: warning logged, `ctx.module_validation_notes` ends up with only secondary notes (if any). No crash.
5. **V3 flag off but `ALFRED_MODULE_SPECIALISTS=1`.** Single-module path runs via `detect_module()`; `ctx.secondary_modules = []`. Zero behavioural diff from V2.
6. **Unknown module in `secondary_modules` list** (e.g. stale state, classifier bug). Skipped silently in provide-contexts and backfill; validation call returns `[]`. Never raises.
7. **Secondary module's rule emits severity=blocker.** Downgraded to warning before the note leaves the validation call. UI sees warning-level from a secondary; Deploy stays enabled.
8. **Redis cache** (from V2.0.1): unchanged — each module's provide_context cache entry is keyed separately, so primary and secondary caches don't collide.

## Testing

### Unit tests

- **`detect_all`** returns primary + up to 2 secondaries.
- **`detect_all`** respects target_doctype priority for primary.
- **`detect_all`** dedups: same module doesn't appear as both primary and secondary.
- **`detect_all`** with no matches returns `(None, "", [])`.
- **`detect_modules`** (orchestrator) returns `ModulesDecision` with correct fields.
- **`detect_module`** (V2 compat) still works — delegates cleanly.
- **Severity capping**: a blocker from a secondary module emerges as a warning in the merged notes list.
- **Backfill**: primary's naming pattern wins; secondary permissions merged without duplicates; unknown secondary module skipped.

### Pipeline integration tests

- PHASES order: `classify_modules` before `provide_module_contexts`, which is before `build_crew`.
- V3 flag on + primary + 1 secondary: `ctx.secondary_modules` populated, `ctx.module_context` contains both headers.
- V3 flag off + V2 flag on: single-module behaviour identical to V2's existing tests.
- Secondary-only blocker does NOT set any deploy-gating flag in payload.
- Primary blocker DOES set it.

### E2E (manual on `dev.alfred`)

- Prompt: *"Create a DocType that links Employee to a Salary Slip item."* Expected: primary=hr, secondaries=[payroll]. Both permissions merged. HR naming pattern used.
- Prompt: *"Add a depreciation-tracking field on an Asset Category."* Expected: primary=assets, secondaries=[] (Accounts is not mentioned explicitly enough — Assets' own permissions already include Accounts User).
- Flag off: same two prompts classify single-module as V2 did.

## Rollout

1. Ship V3 substrate behind `ALFRED_MULTI_MODULE=1`. Merge with flag off. V1 + V2 behaviour unchanged.
2. Enable on `dev.alfred`. Run the three pilot cross-module prompts manually; verify merged output.
3. Watch for false secondaries (keyword match triggers secondary on prompts that aren't actually multi-module). If frequent, tighten the `max_secondaries` cap or add explicit exclusion rules to module KBs.
4. If stable for two sprints, flip the default on. Retire flag-check branches once the V2 single-module path is unused outside tests.

## Open questions

- **Should the LLM classifier ever return secondaries?** V3 keeps LLM to primary only to avoid a token-budget explosion and because the heuristic covers the common cases. If real-world usage shows classifier-primary + heuristic-secondary produces worse output than heuristic-primary + heuristic-secondary, revisit.
- **Per-module weight in merged context?** Currently all secondary contexts get equal weight. Proposal: truncate secondary snippets to ~half the primary's max_tokens budget (200 vs 400) so the LLM stays anchored to the primary domain.
- **Should `detected_module_secondaries` appear in `field_defaults_meta` rationales?** When Accounts Manager is added from the primary (Accounts) and Projects User is added from the secondary (Projects), rationales could say "Added Projects User because the request touches Projects as secondary context." Improves UI transparency. Probably worth including — cheap and informative.
- **User override.** Today the UI doesn't let the user pin or exclude a module before the build runs. If the classifier routinely picks wrong secondaries, a pre-build override would let the user strip them. Out of scope for V3 first cut; revisit based on usage.
