# Insights → Report Handoff — Design

**Date:** 2026-04-22
**Status:** Draft, pending user review
**Scope:** `alfred-processing/alfred/` (orchestrator + handlers/insights + registry + agents) and `alfred_client/` (PreviewPanel + chat widget)
**Prompted by:** Real test prompt *"Show top 10 customers by revenue this quarter"* produced a garbage Server Script changeset because (a) the mode classifier routed analytics-verb prompts to Dev instead of Insights, (b) no Report specialist exists, and (c) the generic Developer hallucinated a validation script when no target DocType was discoverable.

## Problem

Alfred treats two fundamentally different classes of prompt identically:

- **"Show me the data"** — analytics / exploration. The user wants an answer NOW; persistence is optional.
- **"Build me a Report DocType"** — durable. The user wants a saved artifact that other users can re-run.

Alfred today routes both to Dev mode. Dev mode tries to build a changeset. When the prompt is analytics-shaped ("show top 10", "list", "count by", "what are our highest-revenue customers"), the Developer has no DocType to emit, no fields to define, and no concrete target — so it fabricates something. The user gets a broken Server Script with a hallucinated `frappe.throw("Customer is required")` instead of the data they asked for.

Insights mode exists specifically for read-only Q&A, but:
1. The orchestrator's fast-path doesn't catch analytics verbs strongly enough (they leak into Dev).
2. Even when Insights handles the query correctly, there's no path from *"here's the answer"* to *"save this view as a Report DocType for next time"*. If the user wants persistence, they have to re-explain the whole thing to Alfred from scratch.

## Goal

Teach Alfred that **analytics prompts are Insights-first, Report-deployment-second**. Two components:

1. **Mode classifier tightening** so "show", "top N", "list", "count", etc. reliably route to Insights.
2. **Insights → Report handoff**: when an Insights reply represents a report-shaped query (tabular, filterable, aggregation-ready), the preview panel offers a **"Save as Report"** button. Clicking it fires a Dev-mode turn with a structured handoff spec (not a raw re-prompt), classified as `create_report` intent, built by a new Report Builder specialist.

## V1 scope

- **Mode classifier tightening** via deterministic fast-path rules for analytics verbs (~10 patterns). No LLM retraining.
- **Insights handler structured output**: alongside its text reply, emits an optional `report_candidate` dict carrying the query shape (target DocType, columns, filters, aggregations, sort, limit, time range). The existing plain-text reply is unchanged.
- **Client-side "Save as Report" button** in the insights reply surface. Visible only when `report_candidate` is present.
- **"Save as Report" click** dispatches a Dev-mode turn with `intent=create_report` pre-filled and the `report_candidate` serialized into the user prompt as a structured block (not free text).
- **Report Builder intent specialist** at the Dev-mode Developer stage. Pilot intent for non-DocType V1 specialists.
- **`create_report.json` intent registry** with shape-defining fields (report_type, ref_doctype, columns, filters, is_standard, module).
- **Flag**: `ALFRED_REPORT_HANDOFF=1`. Gated additionally on `ALFRED_PER_INTENT_BUILDERS=1`. Off by default. When off, Insights behaves as today (text reply only, no handoff button).

## Non-goals (V1)

- **Script Reports** — only Query Reports (SQL-ish) and Report Builder (field-list) reports ship. Script Reports need Python code and raise sandboxing concerns — V2+.
- **Cross-DocType joins** — Query scope is single DocType + its link-field targets. A report that joins Sales Invoice to Project at runtime is not supported in V1.
- **Scheduled / dashboard-embedded reports** — V2+ feature; V1 just creates the Report DocType.
- **LLM-drafted SQL** — V1 Report Builder uses Frappe's Report Builder (field list + filters), not Query Report (raw SQL). SQL generation is V2.
- **Retroactive conversion of old Insights conversations** — the handoff button appears only on freshly-emitted Insights replies, not on message history.

## Prerequisites

- V1 intent specialist substrate (`ALFRED_PER_INTENT_BUILDERS=1`) must be on. Report Builder uses the same dispatch pattern as DocType Builder.
- Existing Insights mode handler (`alfred/handlers/insights.py`) must be working. V1 extends it; does not rewrite.
- Existing mode classifier (`alfred/orchestrator.py::classify_mode`) — V1 adds fast-path rules to it without changing the LLM-classifier contract.

## Architecture

**Before (today's broken path):**
```
"Show top 10 customers by revenue this quarter"
  -> classify_mode -> "dev" (misrouted; fast-path misses "show")
  -> dev crew runs
  -> Developer has no anchor, fabricates a validation Server Script
  -> user sees garbage
```

**After (V1):**
```
"Show top 10 customers by revenue this quarter"
  -> classify_mode -> "insights" (NEW fast-path rule catches "show top N")
  -> Insights handler runs:
       - calls MCP tools, resolves the query against the live site
       - returns: {
             reply: "<natural language: your top 10 customers are...>",
             report_candidate: {
                 target_doctype: "Customer",
                 report_type: "Report Builder",
                 columns: [...], filters: [...], aggregations: [...],
                 sort: [...], limit: 10, time_range: "this_quarter"
             }
         }
  -> WebSocket emits insights_reply type with reply + report_candidate
  -> Preview panel renders:
       <reply as chat message>
       [Save as Report]  <-- NEW button (only when report_candidate present)
  -> User clicks [Save as Report]:
       -> Client dispatches new Dev-mode turn with:
              prompt = <templated from report_candidate>
              forced_intent = "create_report"
              forced_classifier_source = "handoff"
       -> Dev pipeline runs:
            classify_intent forced to "create_report"
            classify_module detects target = "Customer" -> module = "selling"
            build_alfred_crew(intent="create_report", module="selling",
                              module_context=<selling snippet>)
              -> Report Builder specialist Agent
              -> generate_changeset emits:
                 {"op": "create", "doctype": "Report",
                  "data": {"ref_doctype": "Customer", "report_type": "Report Builder",
                           "columns": [...], "filters": [...], ...}}
       -> backfill + module validation + WebSocket -> changeset preview
  -> User approves -> Report DocType deployed; visible at /app/report/<name>.
```

## Components

### A. Mode classifier fast-path tightening (MODIFIED)

Location: `alfred/orchestrator.py::classify_mode`.

Add to the existing fast-path block (the pre-LLM heuristic matcher) patterns that deterministically route to Insights:

```python
_INSIGHTS_FAST_PATH_PATTERNS = (
    # Tabular / aggregation verbs
    r"\bshow (me |us )?(the )?top \d+",
    r"\blist (the |all |my )?",
    r"\bcount of\b",
    r"\bhow many\b",
    r"\bwhat (is|are) (the |my |our )?",
    r"\bgive me (the |a )?(list|count|summary|total)",
    r"\breport (me |us )?(on|the)",
    r"\bsummar(y|ise|ize)",
)
```

Runs before the LLM classifier. When any pattern matches, `classify_mode` returns `ModeDecision(mode="insights", source="fast_path", ...)`. No LLM call. Cheap and deterministic.

Test-only override: if the prompt explicitly includes verbs like "build", "create a Report DocType", "save as report", these beat the Insights pattern (user explicitly wants deployment). Ordering: deploy-verb wins.

### B. Insights handler structured output (MODIFIED)

Location: `alfred/handlers/insights.py`.

Today returns a plain string reply. V1 extends to an object:

```python
@dataclass
class InsightsResult:
    reply: str                              # natural-language answer (unchanged)
    report_candidate: dict | None = None    # optional structured handoff
    data_preview: list[dict] | None = None  # optional sample rows
```

Backward compat: existing callers that treat the return as a string wrap via `result.reply` or a `__str__` shim. Pipeline's `_run_insights_short_circuit` is updated to emit both fields in the WebSocket message.

The `report_candidate` shape:

```json
{
  "target_doctype": "Customer",
  "report_type": "Report Builder",
  "columns": [
    {"fieldname": "customer_name", "label": "Customer"},
    {"fieldname": "customer_group", "label": "Group"},
    {"fieldname": "revenue", "label": "Revenue (YTD)", "source": "aggregation:sum:grand_total:linked_sales_invoice"}
  ],
  "filters": [
    {"fieldname": "status", "operator": "=", "value": "Active"}
  ],
  "sort": [{"fieldname": "revenue", "direction": "desc"}],
  "limit": 10,
  "time_range": {"field": "posting_date", "preset": "this_quarter"}
}
```

Emission rule — handler emits `report_candidate` only when:
1. The query resolved to a single target DocType (or a DocType + aggregations via linked docs).
2. The query returned tabular data (>= 1 row, each row a homogeneous dict).
3. The query has at least one explicit sort / aggregation / limit — i.e. it's shaped like a "top N" / "by X" query, not a pure single-value answer.

Questions that are NOT report candidates (don't emit the block): *"What's our total revenue?"* (single scalar), *"When was the last sales invoice?"* (single document), *"Is X a submittable DocType?"* (metadata).

### C. `create_report.json` intent registry (NEW)

Location: `alfred/registry/intents/create_report.json`.

Follows V1 intent registry shape:

```json
{
  "intent": "create_report",
  "display_name": "Create Report",
  "doctype": "Report",
  "fields": [
    {
      "key": "ref_doctype",
      "label": "Source DocType",
      "type": "link",
      "link_doctype": "DocType",
      "required": true
    },
    {
      "key": "report_type",
      "label": "Report type",
      "type": "select",
      "options": ["Report Builder", "Query Report", "Script Report"],
      "default": "Report Builder",
      "rationale": "Report Builder is the safest default: field-list + filters, no SQL or Python. Promote to Query Report only when aggregations require raw SQL; Script Report is V2+ only."
    },
    {
      "key": "is_standard",
      "label": "Standard?",
      "type": "check",
      "default": 0,
      "rationale": "Standard reports live in an app's filesystem and require a module + file write. Default to non-standard (site-local) unless the user is shipping the report in an app."
    },
    {
      "key": "module",
      "label": "Module",
      "type": "link",
      "link_doctype": "Module Def",
      "required": true
    }
  ]
}
```

(Columns, filters, and sort rows aren't shape-defining in the registry sense — they're payload passed through from the handoff candidate. Validation happens in the Report Builder specialist, not in the registry.)

### D. Report Builder intent specialist (NEW)

Location: `alfred/agents/builders/report_builder.py`.

Mirrors `doctype_builder.py`:

- `_REPORT_BACKSTORY` — rich domain backstory for Report DocType creation (knows Report Builder vs Query Report vs Script Report distinctions, knows `ref_doctype` / `columns` / `filters` / `sort` / `letter_head`).
- `render_registry_checklist(schema)` — renders the `create_report.json` fields as a checklist.
- `build_report_builder_agent(site_config, custom_tools)` — specialist Agent.
- `enhance_generate_changeset_description(base, module_context="")` — appends Report-specific checklist to the base Developer prompt.

Dispatch in `alfred/agents/crew.py::_get_specialist_developer_agent`:

```python
if intent == "create_report":
    from alfred.agents.builders.report_builder import build_report_builder_agent
    agent = build_report_builder_agent(site_config=site_config, custom_tools=custom_tools)
    logger.info("Builder specialist selected: intent=%s agent_role=%r", intent, agent.role)
    return agent
```

Same in `_enhance_task_description` to route to the Report Builder's prompt enhancer for `intent == "create_report"`.

### E. Intent classifier extension (MODIFIED)

Location: `alfred/orchestrator.py`.

Add `"create_report"` to `_SUPPORTED_INTENTS`. Add heuristic patterns:

```python
_HEURISTIC_INTENT_PATTERNS["create_report"] = (
    "save as report", "save this as a report",
    "create a report", "make a report",
    "build a report", "new report",
)
```

"save as report" is the phrase the client will inject on handoff. The others catch organic direct requests.

### F. Client: "Save as Report" button + handoff dispatch (NEW)

Location: `alfred_client/alfred_client/public/js/alfred_chat/` — the chat widget (wherever Insights replies are rendered; likely `MessageBubble.vue` or similar) and `PreviewPanel.vue` for the actual Report preview.

Changes:
1. Insights reply rendering reads `report_candidate` from the WebSocket payload. When present, renders a "Save as Report" button alongside the reply bubble.
2. Click handler dispatches a new Dev-mode prompt. The prompt is templated from the candidate:

   ```
   Save as Report:
   Source DocType: Customer
   Report type: Report Builder
   Columns: Customer Name, Group, Revenue (YTD)
   Filters: status = Active
   Sort: Revenue DESC, limit 10
   Time range: posting_date in this_quarter
   ```

   Plus a hidden `__report_candidate__` JSON block the pipeline can parse if present. Intent classifier fast-path sees "save as report" and tags the prompt `intent=create_report`.

3. Preview panel renders the resulting Report DocType changeset the same way any other DocType changeset renders (V1 default pills, V2 module badge if applicable).

### G. Pipeline hooks for forced intent from handoff (MODIFIED)

Location: `alfred/api/pipeline.py`.

When a prompt includes the hidden `__report_candidate__` JSON block (or equivalent handoff marker), the pipeline:
1. Parses the candidate and stores on `ctx.report_candidate: dict | None`.
2. `_phase_classify_intent` short-circuits to `intent="create_report", source="handoff", confidence="high"` when the marker is present. No LLM call.
3. Report Builder specialist sees `ctx.report_candidate` via the task description template (interpolated alongside `{prompt}`, `{design}`, etc.) so it emits a faithful changeset instead of re-interpreting.

This is the architectural key: the handoff is a *structured contract*, not a rebuilt prompt. The Report Builder doesn't re-parse "top 10 customers" — it gets the already-resolved column / filter list verbatim.

## Data flow

```
1. user -> [client]        "Show top 10 customers by revenue this quarter"
2. [client] -> ws ->       [processing]
3. classify_mode fast-path -> mode="insights"
4. Insights handler:
      runs MCP tools (lookup_doctype, site-query), builds reply + candidate
      returns: {reply: "<top 10 as prose + table>", report_candidate: {...}}
5. ws emit: {type: "insights_reply", data: {reply, report_candidate, ...}}
6. [client] renders chat bubble with reply + [Save as Report] button

--- user clicks [Save as Report] ---

7. [client] -> ws ->       Dev-mode prompt templated from candidate +
                           __report_candidate__ JSON block
8. classify_mode -> "dev"
9. classify_intent:
      handoff marker present -> short-circuit intent="create_report"
10. classify_module:
      target_doctype="Customer" (extracted) -> module="selling"
11. provide_module_context: selling snippet
12. build_alfred_crew(intent="create_report", module="selling",
                     module_context=<snippet>):
       - Report Builder specialist picked (logged)
       - Task description enhanced with intent checklist + module context
       - Developer emits: [{op: create, doctype: Report, data: {ref_doctype: "Customer",
                            report_type: "Report Builder", columns: [...], filters: [...], ...}}]
13. post_crew:
      backfill_defaults_raw:
         intent defaults: report_type=Report Builder, is_standard=0
         primary module (selling): Sales Manager/User perms on the Report
         (N/A — Report DocType permissions are system-level, not per-module)
      validate_output(selling): 0-1 notes about Report conventions
14. ws emit changeset -> preview panel -> user approves -> Report deployed
```

## Error handling

1. **Mode fast-path matches but prompt is actually a deploy request.** Example: *"Show me a report DocType that lists top customers"*. The explicit "report DocType" / "deploy" / "build" override wins. Fast-path consults deploy-verb patterns first.

2. **Insights handler can't form a report_candidate** (single scalar answer, no tabular data). Reply-only; client shows no button. Current behavior.

3. **User clicks "Save as Report" but classifier loses context.** The handoff marker in the prompt body is the source of truth. If the marker is missing / malformed, classifier falls back to keyword heuristic (`save as report` → create_report). If that also misses, LLM fallback runs; if it returns unknown, generic Developer takes over (may fail — but not worse than today).

4. **Report Builder specialist emits malformed changeset.** Same extraction-and-rescue path as any Dev failure. Post-crew validation catches missing `ref_doctype` / unsupported `report_type`.

5. **V1 flag off (`ALFRED_REPORT_HANDOFF=0`).** Insights returns text-only (current behavior). No button rendered. No classifier changes. No Report Builder specialist dispatch — intent="create_report" still works if the user directly asks, but there's no handoff plumbing.

6. **Handoff fires but V1 intent specialists flag is off (`ALFRED_PER_INTENT_BUILDERS=0`).** `create_report` classification still happens, but generic Developer runs instead of Report specialist. Likely produces a valid-ish Report DocType via its existing prompt, but without module context or the registry checklist. Degraded, not broken.

## Testing

### Unit tests

**Mode classifier fast-path (`tests/test_orchestrator.py` extensions):**
- "show top 10 customers" → mode="insights", source="fast_path"
- "list all suppliers" → mode="insights", source="fast_path"
- "build a Report DocType for top customers" → mode="dev", source="fast_path" (deploy verb wins)
- "how many sales orders this month" → mode="insights"

**Insights handler structured output (`tests/test_insights_handler.py` extensions):**
- A top-N prompt emits `report_candidate` with target_doctype, columns, sort, limit.
- A scalar-answer prompt (*"what's our total revenue"*) emits NO `report_candidate`.
- A metadata prompt (*"is X submittable?"*) emits NO `report_candidate`.

**Intent classifier with "save as report" (`tests/test_classify_intent.py` extensions):**
- "save this as a report" heuristic → intent="create_report", source="heuristic".
- Direct `_SUPPORTED_INTENTS` listing.

**Report Builder specialist (`tests/test_report_builder.py` NEW):**
- `build_report_builder_agent()` returns Agent with Report-domain backstory.
- `enhance_generate_changeset_description(base)` contains the checklist.
- Idempotent.

**Create_report registry (`tests/test_registry_meta_schema.py` auto-picks up):**
- `create_report.json` validates against meta-schema.
- `IntentRegistry.get("create_report")` returns expected fields.

**Pipeline handoff (`tests/test_pipeline_handoff.py` NEW):**
- Prompt with `__report_candidate__` marker → `ctx.report_candidate` populated + intent forced to `create_report`.
- Prompt without marker + "save as report" phrase → heuristic-path intent match still fires.

### Integration tests

- `tests/test_e2e_insights_handoff.py` (stubbed LLM):
  - Stage 1: send "show top 10 customers by revenue" → mode=insights, handler returns reply + candidate.
  - Stage 2: client simulates "save as report" click → pipeline classifies intent=create_report, Report specialist runs, emits Report DocType changeset with ref_doctype=Customer.
  - Flag-off regression: both flags off → stage 1 returns text-only, no candidate, no button.

### Manual E2E on `dev.alfred`

Enable all flags: `ALFRED_PER_INTENT_BUILDERS=1 ALFRED_MODULE_SPECIALISTS=1 ALFRED_REPORT_HANDOFF=1 ./dev.sh`.

- *"Show top 10 customers by revenue this quarter"* → Insights reply renders inline with a Save as Report button. Click → Dev changeset with a Report DocType targeting Customer. Approve → report visible at `/app/report/<name>`.
- *"What's our total revenue?"* → Insights text reply only. No button.
- *"Build a report DocType named Top Customers listing customer name and revenue"* → goes straight to Dev (deploy verb present), skips Insights, produces Report changeset via the same Report specialist.

## Rollout

1. Ship mode-classifier tightening first (component A) as a standalone PR. Low risk, immediate UX improvement.
2. Ship Insights structured output (B) + create_report registry (C) + Report Builder specialist (D) together.
3. Ship the classifier intent extension (E) + client handoff button (F) + pipeline handoff hook (G).
4. Enable flag on `dev.alfred`. Calibrate: Insights handler needs to reliably identify when a prompt is report-shaped vs. not.
5. Once two sprints of stable calibration, flip flag default on. Retire flag-check branches once Insights text-only path is unused.

## Open questions

- **Should `report_candidate` include `columns[].source` describing aggregations, or emit column defs the user can edit before save?** V1: include source metadata so the Report Builder specialist can generate the correct column spec deterministically. UI editability is V2.
- **Where does the "Save as Report" button live visually?** Below the chat reply bubble, inline with the text. Alternative: a persistent action bar. Decide during implementation. Recommend inline-below-bubble — matches Plan mode's "Approve & Build" pattern.
- **Should the handoff prompt include a proposed report name?** V1: handler suggests one (*"Top Customers by Revenue - This Quarter"*); user sees it in the Report changeset preview before approval and can edit if they want. Re-prompting for a name would double the round-trips.
- **Multi-module reports (Sales Invoice → Customer → Territory)?** V1 scopes to single target DocType + its link fields. Multi-module reports are a V3 Multi-Module Classification concern — not this spec.
- **What if the user's site has no Sales Invoice data?** Insights reports "No data matches". No `report_candidate` emitted (no rows = nothing to save). User adjusts filters and retries.

## Bottom line

- **Users get the right UX**: analytics prompts show data first, deploy only if persistence is needed.
- **Stops the hallucinated-Server-Script failure mode** by never routing analytics prompts to Dev in the first place.
- **Extends V1 intent specialists** with the second specialist (Report Builder), the first non-DocType example. Template shape validated for future intents (Server Script, Workflow, Dashboard).
- **Compatible with V2 module specialists and V3 multi-module**: Report Builder accepts module_context like any other specialist; handoff participates in the flag matrix cleanly.
