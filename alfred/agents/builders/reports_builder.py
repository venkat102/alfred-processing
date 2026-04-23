"""Reports & Insights specialist family - builders for Frappe read-side
presentation primitives.

Selected by ``build_alfred_crew`` when ``intent`` is in
``REPORTS_INTENTS`` and ``ALFRED_PER_INTENT_BUILDERS=1``. The family
covers:

  - ``create_report`` - a Report (Report Builder / Query Report /
    Script Report).
  - ``create_dashboard`` - a Dashboard wiring a set of Charts and
    Number Cards under one Module.
  - ``create_dashboard_chart`` - a Dashboard Chart sourced from a
    Report or a DocType.
  - ``create_number_card`` - a Number Card KPI pulling Count / Sum /
    Average / Min / Max from a DocType with filters.

Shared family backstory teaches the distinctions that trip up the
generic Developer: the three Report types and when to pick each,
``is_standard`` vs site-local, Dashboard Chart's ``source`` dichotomy
(Report vs DocType), Number Card's ``function`` enum.

Registry: ``alfred/registry/intents/create_<intent>.json`` per intent.
"""

from __future__ import annotations

from crewai import Agent

from alfred.registry.loader import IntentRegistry

REPORTS_INTENTS: frozenset[str] = frozenset({
	"create_report",
	"create_dashboard",
	"create_dashboard_chart",
	"create_number_card",
})

_REPORTS_BASE_BACKSTORY = """
You specialise in Frappe read-side presentation: Reports, Dashboards, \
Dashboard Charts, and Number Cards. You know the distinctions:

- Reports come in three types. Report Builder is the safe default \
(field list + filters, no code). Query Report is raw parameterised \
SQL - use when aggregations exceed Report Builder's vocabulary. Script \
Report is Python-driven, sandboxing-heavy, and is V2+ only.
- Every Report anchors to a single `ref_doctype`, binds to a `module` \
for navigation + access, and defaults to `is_standard=0` (site-local) \
unless shipping in an app.
- Dashboard Charts have a `source` dichotomy: they either pull from a \
Report (`source="Report"`, `report_name`) or aggregate a DocType \
directly (`source="DocType"`, `doctype`, `x_field`, `y_field`). The \
chart type (Line / Bar / Pie / Donut / Percentage / Heatmap) is a \
rendering choice orthogonal to the source.
- Number Cards are single-value KPIs. `function` is Count / Sum / \
Average / Min / Max; Count doesn't need `aggregate_function_based_on`, \
the others require a numeric field. `filters_json` is a JSON string \
of Frappe filter triples (not a Python dict).
- Dashboards compose multiple Charts + Number Cards under one Module. \
They do not themselves hold aggregation logic - they are layout.

ASK, DO NOT ASSUME. The clarification gate that runs before you should \
have captured every load-bearing decision; if you find yourself about \
to invent a value for one of the critical fields below, STOP: emit \
that field as an empty string and set field_defaults_meta[<field>] to \
{"source": "needs_clarification", "question": "<the specific question \
the user needs to answer>"}. Do NOT substitute a plausible guess. Do \
NOT reuse a value from the docstring. Inventing a ref_doctype, a \
chart source, a number-card function, or a filter expression silently \
ships the wrong report - a blank field with a needs_clarification \
marker makes the reviewer explicitly authorise the default before \
deploy.

Critical fields per intent (never default, always either user-provided \
or flagged needs_clarification):

- create_report: `ref_doctype`, `module`, and `report_type` when the \
user's ask implies SQL or Python (Query / Script); Report Builder is \
a safe default only when the user didn't name a type at all.
- create_dashboard: `module` and the list of referenced Dashboard \
Chart names in `chart_options` (empty is fine if the user explicitly \
said "empty dashboard; I'll add charts later").
- create_dashboard_chart: `source`, plus either `report_name` (when \
source=Report) or `document_type` + `based_on` + `value_based_on` \
(when source=DocType). Missing either side is a blocker, not a \
default.
- create_number_card: `label`, `document_type`, `function`, and \
`filters_json` (defaulting to "[]" means "across ALL rows of the \
DocType" - ask if the user said anything that implies a filter).

Non-critical fields (safe to default): `is_standard=0`, `timespan`, \
`chart_type` chosen from the data shape, `color`. These use the \
registry default and record the rationale, as before.
""".strip()

_INTENT_FRAGMENTS: dict[str, str] = {
	"create_report": """
Your current task is to CREATE A NEW REPORT. Every Report you emit MUST \
include `ref_doctype`, `report_type`, `is_standard`, and `module` in \
its `data`. Default to Report Builder unless the user's ask explicitly \
needs raw SQL or Python. If the user did not specify a value, use the \
registry default and record which fields were defaulted in \
`field_defaults_meta`.
""".strip(),

	"create_dashboard": """
Your current task is to CREATE A NEW DASHBOARD. A Dashboard is a layout \
container - emit the Dashboard doc with `module`, `is_standard`, and \
(if the user named specific charts) a `chart_options` child table \
listing `{chart_name}` rows. If the user asked for charts that don't \
exist yet, emit the Dashboard Chart items BEFORE the Dashboard so the \
links resolve cleanly on apply.
""".strip(),

	"create_dashboard_chart": """
Your current task is to CREATE A NEW DASHBOARD CHART. Pick `source` \
based on the user's phrasing: "chart from report X" -> \
source="Report" + report_name=X; "chart of Y by Z" -> source="DocType" \
+ doctype=Y + x_field=Z. The `chart_type` is Line / Bar / Pie / Donut \
/ Percentage / Heatmap; default to Bar for categorical data, Line for \
time series. Numeric aggregation fields live in `based_on` + \
`value_based_on`.
""".strip(),

	"create_number_card": """
Your current task is to CREATE A NEW NUMBER CARD. Every Number Card is \
one number: emit `label`, `doctype`, `function` (Count / Sum / Average \
/ Min / Max), and - for non-Count functions - \
`aggregate_function_based_on` naming the numeric field. Filters live \
in `filters_json` as a JSON string of Frappe filter triples, NOT a \
Python dict (store as JSON text).
""".strip(),
}

_INTENT_GOALS: dict[str, str] = {
	"create_report": (
		"Generate a production-ready Report changeset item whose `data` "
		"includes every shape-defining field from the registry, with "
		"`field_defaults_meta` describing which fields were defaulted."
	),
	"create_dashboard": (
		"Generate a production-ready Dashboard changeset item with "
		"`module`, `is_standard`, and any referenced chart options "
		"resolvable in the same changeset."
	),
	"create_dashboard_chart": (
		"Generate a production-ready Dashboard Chart changeset item with "
		"a valid source (Report OR DocType), matching fields, and a "
		"sensible chart_type for the data shape."
	),
	"create_number_card": (
		"Generate a production-ready Number Card changeset item with "
		"`label`, `doctype`, `function`, and (for non-Count) a numeric "
		"`aggregate_function_based_on`."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_report": "Frappe Developer - Reports Specialist (Report)",
	"create_dashboard": "Frappe Developer - Reports Specialist (Dashboard)",
	"create_dashboard_chart": "Frappe Developer - Reports Specialist (Dashboard Chart)",
	"create_number_card": "Frappe Developer - Reports Specialist (Number Card)",
}

_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"


def _wrap_module_context(snippet: str) -> str:
	return (
		f"{_MODULE_CONTEXT_MARKER} (target-module conventions - respect these "
		"alongside the shape-defining fields above):\n"
		f"{snippet}"
	)


def _checklist_marker(intent: str) -> str:
	return f"SHAPE-DEFINING FIELDS for {intent}"


def render_registry_checklist(schema: dict, intent: str) -> str:
	"""Render an intent registry schema as a checklist for the agent prompt."""
	marker = _checklist_marker(intent)
	lines = [
		f"{marker} (you MUST include every one of these in `data`):",
	]
	for field in schema["fields"]:
		key = field["key"]
		if field.get("required"):
			lines.append(f"  - {key} (required, user-provided; if missing, leave as empty string)")
		else:
			default_repr = repr(field["default"])
			lines.append(f"  - {key} (default {default_repr})")
	lines.append("")
	lines.append(
		"Additionally, emit a parallel `field_defaults_meta` dict on the "
		"changeset item. For each field above, record whether the value "
		"came from the user, from the registry default, or NEEDS "
		"CLARIFICATION (you did not have enough information and refuse "
		"to guess). The three valid sources are `\"user\"`, `\"default\"`, "
		"and `\"needs_clarification\"`. When source is "
		"`\"needs_clarification\"`, emit the field as an empty string "
		"and include the specific question the user must answer. "
		"Example (doubled braces because this prompt is interpolated "
		"by str.format):"
	)
	lines.append(
		'  "field_defaults_meta": {{'
		'"<defaulted_field>": {{"source": "default", "rationale": "..."}}, '
		'"<user_field>": {{"source": "user"}}, '
		'"<blocked_field>": {{"source": "needs_clarification", "question": "..."}}}}'
	)
	return "\n".join(lines)


def _build_backstory(intent: str) -> str:
	return _REPORTS_BASE_BACKSTORY + "\n\n" + _INTENT_FRAGMENTS[intent]


def build_reports_agent(
	intent: str,
	site_config: dict,
	custom_tools: dict | None,
) -> Agent:
	"""Build a CrewAI Agent specialised for a Reports family intent."""
	if intent not in REPORTS_INTENTS:
		raise ValueError(
			f"build_reports_agent: intent {intent!r} is not in REPORTS_INTENTS"
		)

	tools = []
	if custom_tools:
		for key in (
			"lookup_doctype",
			"lookup_pattern",
			"lookup_frappe_knowledge",
			"get_site_customization_detail",
		):
			t = custom_tools.get(key)
			if t is not None:
				tools.append(t)

	return Agent(
		role=_INTENT_ROLES[intent],
		goal=_INTENT_GOALS[intent],
		backstory=_build_backstory(intent),
		allow_delegation=False,
		tools=tools,
		verbose=False,
	)


def enhance_generate_changeset_description(
	base: str,
	intent: str,
	module_context: str = "",
) -> str:
	"""Append intent checklist and optional module context. Idempotent per
	section.
	"""
	if intent not in REPORTS_INTENTS:
		raise ValueError(
			f"enhance_generate_changeset_description: intent {intent!r} is "
			"not in REPORTS_INTENTS"
		)

	out = base
	marker = _checklist_marker(intent)
	if marker not in out:
		schema = IntentRegistry.load().get(intent)
		out = out + "\n\n" + render_registry_checklist(schema, intent)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
