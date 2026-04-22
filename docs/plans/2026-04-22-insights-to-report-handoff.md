# Insights → Report Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Ship the full feature described in the spec — mode classifier tightening so analytics verbs route to Insights, Insights handler emits optional `report_candidate`, client renders "Save as Report" button, structured handoff to Dev mode with a new Report Builder specialist.

**Architecture:** Extends V1 intent-specialist substrate with `create_report` intent + Report Builder agent. Adds an optional structured-output path to Insights. Adds a handoff marker the pipeline parses to force-classify intent. Single new flag `ALFRED_REPORT_HANDOFF=1` on top of V1's `ALFRED_PER_INTENT_BUILDERS=1`.

**Tech Stack:** Python 3.11, pydantic v2, pytest asyncio, Vue 3. Tabs for Python, line 110.

**Repo:** `/Users/navin/office/frappe_bench/v16/mariadb/alfred-processing`
**Spec:** `docs/specs/2026-04-22-insights-to-report-handoff.md`

---

## File Structure

**New (alfred-processing):**
- `alfred/registry/intents/create_report.json`
- `alfred/agents/builders/report_builder.py`
- `alfred/models/insights_result.py` — `InsightsResult` pydantic + `ReportCandidate` pydantic
- `tests/test_report_builder.py`
- `tests/test_insights_result.py`
- `tests/test_classify_mode_fast_path.py`
- `tests/test_pipeline_report_handoff.py`

**Modified (alfred-processing):**
- `alfred/orchestrator.py` — Insights fast-path patterns + `create_report` in `_SUPPORTED_INTENTS` + heuristic patterns
- `alfred/handlers/insights.py` — returns `InsightsResult`; builds heuristic `report_candidate`
- `alfred/agents/crew.py` — dispatch `create_report` in `_get_specialist_developer_agent` + `_enhance_task_description`
- `alfred/api/pipeline.py` — `ctx.report_candidate`; `_phase_classify_intent` short-circuits on handoff marker; Insights short-circuit emits structured payload
- `tests/test_classify_intent.py` — extend with `create_report` heuristic case
- `tests/test_crew_specialist_dispatch.py` — extend with `create_report` dispatch case

**Modified (alfred_client):**
- `alfred_client/alfred_client/public/js/alfred_chat/AlfredChatApp.vue` (or wherever Insights replies render) — "Save as Report" button + handoff dispatch
- `alfred_client/alfred_client/public/js/alfred_chat/MessageBubble.vue` — render reply with optional button

---

## Task 1: `create_report.json` intent registry

**Files:** Create `alfred/registry/intents/create_report.json`

- [ ] **Step 1: Write the registry**

```json
{
	"intent": "create_report",
	"display_name": "Create Report",
	"doctype": "Report",
	"fields": [
		{"key": "ref_doctype", "label": "Source DocType", "type": "link", "link_doctype": "DocType", "required": true},
		{"key": "report_type", "label": "Report type", "type": "select", "options": ["Report Builder", "Query Report", "Script Report"], "default": "Report Builder", "rationale": "Report Builder is the safest default: field-list + filters, no SQL or Python. Promote to Query Report only when aggregations require raw SQL; Script Report is V2+ only."},
		{"key": "is_standard", "label": "Standard?", "type": "check", "default": 0, "rationale": "Standard reports live in an app's filesystem and require a module + file write. Default to non-standard (site-local) unless the user is shipping the report in an app."},
		{"key": "module", "label": "Module", "type": "link", "link_doctype": "Module Def", "required": true}
	]
}
```

- [ ] **Step 2: Run** `.venv/bin/python -m pytest tests/test_registry_meta_schema.py -v` — expect both V1 registry tests still pass + create_report validates.

- [ ] **Step 3: Commit**

```bash
git add alfred/registry/intents/create_report.json
git commit -m "feat(registry): add create_report intent schema"
```

---

## Task 2: Report Builder specialist

**Files:**
- Create: `alfred/agents/builders/report_builder.py`
- Create: `tests/test_report_builder.py`

- [ ] **Step 1: Write failing tests** mirroring `test_doctype_builder.py`:
  - `render_registry_checklist` lists every field key
  - `build_report_builder_agent` returns Agent with Report-specialist backstory (role contains "Report")
  - `enhance_generate_changeset_description(base)` appends checklist with `ref_doctype`, `report_type`, `module` mentioned
  - Idempotency

- [ ] **Step 2:** Run — expect ImportError.

- [ ] **Step 3: Implement** `alfred/agents/builders/report_builder.py` — copy the shape of `doctype_builder.py`:
  - `_REPORT_BACKSTORY` — Report Builder vs Query Report vs Script Report distinction, `ref_doctype` anchoring, filter/column/sort conventions, Report Builder vs Query Report tradeoff.
  - `_CHECKLIST_MARKER = "SHAPE-DEFINING FIELDS for create_report"`
  - `_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"` (same as DocType variant; shared semantics)
  - `render_registry_checklist(schema)` — identical pattern to DocType variant, using schema from `IntentRegistry.load().get("create_report")`
  - `build_report_builder_agent(site_config, custom_tools)` — `role="Frappe Developer - Report Specialist"`, uses same 4 MCP tools as DocType Builder
  - `enhance_generate_changeset_description(base, module_context="")` — checklist + optional module context, idempotent per-section

- [ ] **Step 4:** Run — expect pass.

- [ ] **Step 5: Commit**

```bash
git add alfred/agents/builders/report_builder.py tests/test_report_builder.py
git commit -m "feat(agents): add Report Builder specialist for create_report intent"
```

---

## Task 3: Extend intent classifier with `create_report`

**Files:** Modify `alfred/orchestrator.py`, extend `tests/test_classify_intent.py`

- [ ] **Step 1: Add failing tests** in `test_classify_intent.py`:
  - `"save this as a report"` → intent="create_report" heuristic
  - `"save as report"` → intent="create_report" heuristic
  - `"create a report"` → intent="create_report" heuristic

- [ ] **Step 2: Modify `alfred/orchestrator.py`**

```python
_SUPPORTED_INTENTS: tuple[str, ...] = ("create_doctype", "create_report")

_HEURISTIC_INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
	"create_doctype": (... unchanged ...),
	"create_report": (
		"save as report",
		"save this as a report",
		"create a report",
		"make a report",
		"build a report",
		"new report",
	),
}
```

- [ ] **Step 3:** Run tests — expect pass, no V2/V3 regressions.

- [ ] **Step 4: Commit**

```bash
git add alfred/orchestrator.py tests/test_classify_intent.py
git commit -m "feat(orchestrator): add create_report to supported intents"
```

---

## Task 4: Dispatch `create_report` in crew.py

**Files:** Modify `alfred/agents/crew.py`, extend `tests/test_crew_specialist_dispatch.py`

- [ ] **Step 1: Add failing tests**
  - `_get_specialist_developer_agent(intent="create_report", ...)` returns Report specialist agent (role contains "Report")
  - `_enhance_task_description("generate_changeset", "create_report", "base", module_context="")` contains "ref_doctype" and "report_type"
  - `_enhance_task_description("generate_changeset", "create_report", "base", module_context="selling snippet")` contains checklist + module context

- [ ] **Step 2: Modify** `_get_specialist_developer_agent` in `crew.py`:

```python
if intent == "create_report":
	from alfred.agents.builders.report_builder import build_report_builder_agent
	agent = build_report_builder_agent(site_config=site_config, custom_tools=custom_tools)
	logger.info("Builder specialist selected: intent=%s agent_role=%r", intent, agent.role)
	return agent
```

And `_enhance_task_description`:

```python
if intent == "create_report":
	from alfred.agents.builders.report_builder import enhance_generate_changeset_description
	return enhance_generate_changeset_description(base_description, module_context=module_context)
```

- [ ] **Step 3:** Run — expect pass.

- [ ] **Step 4: Commit**

```bash
git add alfred/agents/crew.py tests/test_crew_specialist_dispatch.py
git commit -m "feat(crew): dispatch Report Builder specialist for create_report intent"
```

---

## Task 5: Mode classifier fast-path for analytics verbs

**Files:** Modify `alfred/orchestrator.py`, create `tests/test_classify_mode_fast_path.py`

- [ ] **Step 1: Write failing tests**

Covering:
- "show top 10 customers by revenue this quarter" → mode=insights source=fast_path
- "list all suppliers" → mode=insights
- "how many sales orders this month" → mode=insights
- "what are our top accounts" → mode=insights
- "count of customers" → mode=insights
- "build a Report DocType for top customers" → mode=dev (deploy verb wins)
- "create a report that lists our customers" → mode=dev (deploy verb wins)

- [ ] **Step 2:** Run — expect fail on the new Insights-routing cases (fast-path currently misses them).

- [ ] **Step 3: Modify `classify_mode` in `alfred/orchestrator.py`**

Find the existing fast-path block (the pre-LLM heuristic matcher). Extend:

```python
_INSIGHTS_FAST_PATH_PATTERNS = (
	r"\bshow (me |us )?(the )?top \d+",
	r"\blist (the |all |my )?",
	r"\bcount of\b",
	r"\bhow many\b",
	r"\bwhat (is|are) (the |my |our )?",
	r"\bgive me (the |a )?(list|count|summary|total)",
	r"\breport (me |us )?(on|the)",
	r"\bsummar(y|ise|ize)",
)

_DEPLOY_VERB_PATTERNS = (
	r"\bbuild (a |the )?(report|doctype|workflow|server script)",
	r"\bcreate (a |the )?(report|doctype|workflow|server script)",
	r"\bdeploy\b", r"\bsave as report\b",
)


def _fast_path_mode(prompt: str) -> str | None:
	"""Return 'insights' / 'dev' / None based on deterministic verb patterns.

	Deploy-verb override beats analytics verbs so
	"build a Report DocType for top customers" routes to Dev.
	"""
	low = (prompt or "").lower()
	if any(re.search(p, low) for p in _DEPLOY_VERB_PATTERNS):
		return "dev"
	if any(re.search(p, low) for p in _INSIGHTS_FAST_PATH_PATTERNS):
		return "insights"
	return None
```

Splice into `classify_mode` before the LLM-classifier call. If `_fast_path_mode(prompt)` returns a mode, return `ModeDecision(mode=..., source="fast_path", ...)` immediately.

- [ ] **Step 4:** Run — all pass.

- [ ] **Step 5: Commit**

```bash
git add alfred/orchestrator.py tests/test_classify_mode_fast_path.py
git commit -m "feat(orchestrator): analytics verbs fast-path to Insights; deploy verbs override"
```

---

## Task 6: `InsightsResult` + `ReportCandidate` models

**Files:**
- Create: `alfred/models/insights_result.py`
- Create: `tests/test_insights_result.py`

- [ ] **Step 1: Write failing tests**
  - `ReportCandidate` pydantic accepts target_doctype (required), report_type, columns, filters, sort, limit, time_range
  - `ReportCandidate.to_handoff_prompt()` renders a human-readable block suitable for injecting into a Dev-mode prompt
  - `InsightsResult(reply="x")` works with just a reply, `report_candidate=None`
  - `InsightsResult(reply="x", report_candidate=...)` preserves both
  - Both serialize cleanly via `.model_dump()`

- [ ] **Step 2: Implement**

```python
# alfred/models/insights_result.py
from __future__ import annotations
from typing import Any, Optional
from pydantic import BaseModel, Field


class ReportCandidate(BaseModel):
	"""Structured query shape that an Insights handler can emit alongside its
	natural-language reply. Carries everything a Report Builder specialist
	needs to emit a Report DocType changeset deterministically - no re-
	interpretation of English required on the handoff.

	Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
	"""

	target_doctype: str
	report_type: str = "Report Builder"
	columns: list[dict[str, Any]] = Field(default_factory=list)
	filters: list[dict[str, Any]] = Field(default_factory=list)
	sort: list[dict[str, Any]] = Field(default_factory=list)
	limit: Optional[int] = None
	time_range: Optional[dict[str, Any]] = None
	suggested_name: Optional[str] = None

	def to_handoff_prompt(self) -> str:
		parts = [
			f"Save as Report:",
			f"Source DocType: {self.target_doctype}",
			f"Report type: {self.report_type}",
		]
		if self.suggested_name:
			parts.append(f"Suggested name: {self.suggested_name}")
		if self.columns:
			parts.append("Columns: " + ", ".join(
				c.get("label") or c.get("fieldname", "?") for c in self.columns
			))
		if self.filters:
			parts.append("Filters: " + ", ".join(
				f"{f.get('fieldname')} {f.get('operator', '=')} {f.get('value')}"
				for f in self.filters
			))
		if self.sort:
			parts.append("Sort: " + ", ".join(
				f"{s.get('fieldname')} {s.get('direction', 'asc').upper()}"
				for s in self.sort
			))
		if self.limit:
			parts.append(f"Limit: {self.limit}")
		if self.time_range:
			rng = self.time_range
			parts.append(
				f"Time range: {rng.get('field', 'date')} in "
				f"{rng.get('preset', rng.get('value', ''))}"
			)
		return "\n".join(parts)


class InsightsResult(BaseModel):
	"""Return shape for ``handle_insights``. ``report_candidate`` is emitted
	when the query is report-shaped (tabular, filterable, aggregation-ready)
	and the site returned data; None otherwise.
	"""

	reply: str
	report_candidate: Optional[ReportCandidate] = None
```

- [ ] **Step 3:** Run — expect pass.

- [ ] **Step 4: Commit**

```bash
git add alfred/models/insights_result.py tests/test_insights_result.py
git commit -m "feat(models): add InsightsResult and ReportCandidate for handoff"
```

---

## Task 7: Insights handler returns `InsightsResult` with heuristic candidate

**Files:** Modify `alfred/handlers/insights.py`, extend `tests/test_insights_handler.py` if it exists (else create `tests/test_insights_handler_structured.py`)

- [ ] **Step 1: Write failing tests**
  - Handler called with a "top N" prompt returns `InsightsResult` with `report_candidate` populated (target_doctype derived from prompt; limit = 10; sort populated; time_range detected for "this quarter")
  - Handler called with a scalar prompt ("what's our total revenue") returns `InsightsResult` with `report_candidate=None`
  - Backward compat: str(result) returns the reply (if we add `__str__`), or callers that do `result.reply` still work

- [ ] **Step 2: Modify `handle_insights`**

Change return annotation from `-> str` to `-> InsightsResult`. Keep the existing agent-run + reply extraction logic. After building `reply`, call a new helper:

```python
from alfred.handlers.insights_candidate import extract_report_candidate  # new module

async def handle_insights(...) -> InsightsResult:
	reply = ...  # existing logic unchanged
	candidate = extract_report_candidate(prompt=prompt, reply=reply)
	return InsightsResult(reply=reply, report_candidate=candidate)
```

Create `alfred/handlers/insights_candidate.py` with a heuristic extractor:

```python
"""Heuristic report_candidate extraction.

V1 is prompt-driven (parses the user's English), not reply-driven. If a
later iteration wants structured output from the Insights LLM itself, the
extractor can be swapped for an LLM-structured-output path behind the
same interface.
"""
import re
from alfred.models.insights_result import ReportCandidate

_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)
_TIME_RANGE_PRESETS = {
	"today": "today",
	"this week": "this_week", "last week": "last_week",
	"this month": "this_month", "last month": "last_month",
	"this quarter": "this_quarter", "last quarter": "last_quarter",
	"this year": "this_year", "last year": "last_year",
	"ytd": "year_to_date", "year to date": "year_to_date",
}


def extract_report_candidate(*, prompt: str, reply: str) -> ReportCandidate | None:
	"""Return a ReportCandidate when the prompt is report-shaped, else None.

	Heuristic rules:
	- Prompt must contain a "top N" or "list/show all" phrase.
	- Prompt must mention a target entity we can map to a DocType
	  (for now: exact target_doctype_matches from the ModuleRegistry).
	- Reply must not be obviously an error ("I don't know...").
	"""
	low = (prompt or "").lower()
	reply_low = (reply or "").lower()

	# Obvious-error escape hatch
	if any(m in reply_low for m in ("i don't know", "no data", "couldn't find", "not found")):
		return None

	from alfred.registry.module_loader import ModuleRegistry
	registry = ModuleRegistry.load()

	# Target DocType detection: look for any registry target_doctype_match
	# verbatim in the prompt (case-insensitive).
	target = None
	for kb in registry._by_module.values():
		for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
			if dt.lower() in low:
				target = dt
				break
		if target:
			break
	if target is None:
		# Plural fallback: "customers" -> "Customer"
		for kb in registry._by_module.values():
			for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
				if dt.lower() + "s" in low:
					target = dt
					break
			if target:
				break
	if target is None:
		return None

	# Top-N limit
	limit = None
	m = _TOP_N_RE.search(low)
	if m:
		limit = int(m.group(1))

	# Time range preset
	time_range = None
	for phrase, preset in _TIME_RANGE_PRESETS.items():
		if phrase in low:
			# Pick a reasonable date-like field. posting_date is the common one.
			time_range = {"field": "posting_date", "preset": preset}
			break

	# Must be report-shaped (need at least a limit or time range)
	if limit is None and time_range is None:
		return None

	# Suggested name
	name_parts = []
	if limit:
		name_parts.append(f"Top {limit}")
	name_parts.append(f"{target}s")
	if time_range:
		preset_h = time_range["preset"].replace("_", " ").title()
		name_parts.append(f"- {preset_h}")
	suggested_name = " ".join(name_parts)

	return ReportCandidate(
		target_doctype=target,
		report_type="Report Builder",
		limit=limit,
		time_range=time_range,
		suggested_name=suggested_name,
	)
```

Also update the pipeline's `_run_insights_short_circuit` to handle the new return type.

- [ ] **Step 3:** Run — expect pass.

- [ ] **Step 4: Commit**

```bash
git add alfred/handlers/insights.py alfred/handlers/insights_candidate.py tests/test_insights_handler_structured.py
git commit -m "feat(handlers): Insights returns InsightsResult with heuristic report_candidate"
```

---

## Task 8: Pipeline handoff hook + short-circuit intent classification

**Files:** Modify `alfred/api/pipeline.py`, create `tests/test_pipeline_report_handoff.py`

- [ ] **Step 1: Extend `PipelineContext`**

```python
# V4 report handoff additions. Populated when the user prompt carries a
# __report_candidate__ JSON block (Insights -> Report handoff flow).
report_candidate: dict | None = None
```

- [ ] **Step 2: Update `_run_insights_short_circuit` to emit the structured payload**

Locate where the Insights handler's return is emitted as `insights_reply`. Update to read `.reply` and `.report_candidate` off the new `InsightsResult`. The WebSocket data dict gains:
```
"reply": result.reply,
"report_candidate": result.report_candidate.model_dump() if result.report_candidate else None,
```

- [ ] **Step 3: Short-circuit intent classification on handoff marker**

In `_phase_classify_intent`, before the heuristic/LLM path, check for a `__report_candidate__` JSON block at the end of the prompt:

```python
# Handoff short-circuit: structured Insights -> Report handoff.
# Client attaches a "__report_candidate__" JSON block to the prompt when
# the user clicks "Save as Report". Parse, store on ctx, and force-set
# intent=create_report with source=handoff.
import re as _re
import json as _json
marker = _re.search(r"__report_candidate__\s*:\s*(\{[\s\S]*\})", ctx.prompt)
if marker:
	try:
		ctx.report_candidate = _json.loads(marker.group(1))
	except Exception:
		ctx.report_candidate = None
	ctx.intent = "create_report"
	ctx.intent_source = "handoff"
	ctx.intent_confidence = "high"
	ctx.intent_reason = "__report_candidate__ marker present"
	logger.info(
		"Intent handoff: conversation=%s intent=create_report source=handoff candidate_keys=%s",
		ctx.conversation_id,
		list(ctx.report_candidate.keys()) if ctx.report_candidate else [],
	)
	return
```

- [ ] **Step 4: Tests** — build the `__report_candidate__` marker into a prompt, run `_phase_classify_intent`, assert `ctx.intent == "create_report"` and `ctx.intent_source == "handoff"` and `ctx.report_candidate is not None`. Also test that a prompt WITHOUT the marker falls through to normal classification.

- [ ] **Step 5:** Run — expect pass.

- [ ] **Step 6: Commit**

```bash
git add alfred/api/pipeline.py tests/test_pipeline_report_handoff.py
git commit -m "feat(pipeline): Insights->Report handoff marker short-circuits classify_intent"
```

---

## Task 9: Client — "Save as Report" button in Insights reply

**Files:** Modify `alfred_client/alfred_client/public/js/alfred_chat/` — need to locate the Insights reply rendering path.

- [ ] **Step 1: Locate Insights rendering**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
grep -rn "insights_reply\|InsightsReply\|mode.*insights" alfred_client/public/js/alfred_chat/
```

- [ ] **Step 2: Add button + handoff dispatch**

In the component that renders Insights replies (likely `MessageBubble.vue` or the chat-reply handler in `AlfredChatApp.vue`), add logic:
1. Read `report_candidate` from the `insights_reply` payload.
2. When present, render a `Save as Report` button below the reply bubble.
3. On click, dispatch a new chat message of the form:

```
Save as Report:
Source DocType: <target>
Report type: <type>
Suggested name: <name>
Columns: <...>
Filters: <...>
Sort: <...>
Limit: <limit>
Time range: <field> in <preset>

__report_candidate__: {<json of the candidate>}
```

The `__report_candidate__: {...}` trailer is how the pipeline recognizes this as a handoff (see Task 8).

- [ ] **Step 3: CSS**

```css
.alfred-save-as-report-btn {
	margin-top: 8px;
	padding: 4px 10px;
	background: #eef;
	border: 1px solid #d7dde9;
	border-radius: 4px;
	font-size: 12px;
	cursor: pointer;
}
.alfred-save-as-report-btn:hover { background: #dde; }
```

- [ ] **Step 4: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
git add alfred_client/public/js/alfred_chat/
git commit -m "feat(chat): render Save as Report button when insights_reply carries report_candidate"
```

---

## Task 10: Full V1+V2+V3+V4 regression + manual E2E

- [ ] **Step 1: Full regression**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
.venv/bin/python -m pytest tests/ -q
```

Expected: all existing tests green plus the new ones for Tasks 1-8.

- [ ] **Step 2: Manual E2E**

Enable flags:
```bash
ALFRED_PER_INTENT_BUILDERS=1 ALFRED_MODULE_SPECIALISTS=1 ALFRED_MULTI_MODULE=1 ALFRED_REPORT_HANDOFF=1 ./dev.sh
bench build --app alfred_client
```

Run prompts:
- *"Show top 10 customers by revenue this quarter"* → mode=insights (fast-path), reply renders, Save as Report button visible
- Click Save as Report → new Dev turn, pipeline short-circuits intent=create_report, Report Builder specialist runs, changeset preview shows a Report DocType with `ref_doctype=Customer`
- *"What's our total revenue?"* → mode=insights, reply only (no button — not report-shaped)
- *"Build a Report DocType for top customers"* → mode=dev (deploy-verb override), Report Builder specialist runs

Flag-off regression:
- `ALFRED_REPORT_HANDOFF=0` → Insights returns text-only (no candidate), no button, no handoff

---

## Self-Review

**Spec coverage:**
- A Mode classifier fast-path → Task 5
- B Insights structured output → Tasks 6, 7
- C create_report registry → Task 1
- D Report Builder specialist → Task 2
- E Intent classifier extension → Task 3 (+ Task 4 for dispatch)
- F Client button + handoff dispatch → Task 9
- G Pipeline handoff marker parser → Task 8

**Placeholder scan:** Task 7's heuristic extractor has limits (plural fallback, simple time-range detection). These are acknowledged in the spec's "Open questions" and can be tuned post-V1. Not placeholders — working MVP.

**Type consistency:** `ReportCandidate`, `InsightsResult`, `create_report`, `ctx.report_candidate`, `__report_candidate__` marker — used consistently across tasks.
