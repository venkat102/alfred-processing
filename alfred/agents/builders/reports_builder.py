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
	"create_auto_email_report",
})

_REPORTS_BASE_BACKSTORY = """
You specialise in Frappe read-side presentation: Reports, Dashboards, \
Dashboard Charts, and Number Cards. You know the controller-enforced \
rules and the easy-to-confuse naming:

- Reports come in FOUR types: Report Builder (field list + filters, no \
code, the safe default), Query Report (raw SQL that must pass \
check_safe_sql_query - DDL / DML / multi-statement rejected), Script \
Report (Python executed server-side, requires Script Manager role), \
and Custom Report (renders an existing Report Builder config with \
different columns). is_standard='Yes' requires developer_mode AND \
Administrator at save; non-devs / non-Admins cannot create or edit \
standard reports. Flipping 'Yes'->'No' on a saved standard report is \
also blocked.
- DASHBOARD CHART HAS TWO SEPARATE TYPE FIELDS - do NOT confuse them. \
`chart_type` is the AGGREGATION MODE: Count / Sum / Average / Group \
By / Custom / Report. `type` is the RENDER SHAPE: Line / Bar / \
Percentage / Pie / Donut / Heatmap. A Count chart can render as Line, \
a Group By chart can render as Pie, etc. `chart_type` is set_only_once \
(cannot be changed after first save), so pick the aggregation mode up \
front. For Count / Sum / Average chart_types, required fields are \
document_type + based_on (date) + value_based_on (numeric, Sum / Avg \
only). For Group By, required fields are group_by_based_on + \
group_by_type + aggregate_function_based_on (if group_by_type is Sum \
or Average). For Report chart_type, required fields are report_name + \
either use_report_chart=1 (reuse the Report's built-in chart) or \
x_field + y_axis rows.
- NUMBER CARD HAS A TYPE FIELD for its source: Document Type / Report / \
Custom. For Document Type, required fields are document_type + function \
(Count / Sum / Average / Minimum / Maximum) + aggregate_function_based_on \
(for non-Count). For Report, required fields are report_name + \
report_field + report_function (Count is NOT valid here - for a Count \
card, use type='Document Type' instead). For Custom, required is \
method - a dotted path to a whitelisted Python function returning \
{value, fieldtype, route?}.
- filters_json is a JSON string of Frappe filter triples, NOT a Python \
dict. Empty '[]' means 'across all rows' - if the user said anything \
implying a filter ('pending', 'overdue', 'this month'), that's a blocker, \
not a default.
- Dashboards compose Charts + Number Cards under one Module. is_default \
is a global singleton - setting is_default=1 auto-clears is_default on \
every other Dashboard. is_standard=1 enforces that every referenced \
chart / card MUST also be is_standard=1 (the controller rejects mixing). \
chart_options (JSON string) provides rendering defaults merged into \
every chart at render time.
- Auto Email Report schedules recurring delivery of an existing Report \
via email. TWO silent-failure modes to design around: (a) when \
send_if_data=1 (the default) AND the filtered report returns zero rows, \
the email is SUPPRESSED WITH NO NOTIFICATION - fine for exception \
reports ('send if overdue'), confusing for confirmation reports ('send \
weekly status even if clean'); (b) the scheduler MUST be running for \
Auto Email Reports to fire - no scheduler means no email, and no \
in-band error. Script Reports running via Auto Email Report need their \
`roles` field to include a role the scheduler worker holds, otherwise \
the background job fails silently. Use from_date_field + \
dynamic_date_filters_based_on for rolling windows (last 7 days, last \
month) so the same job makes sense week-over-week without manual \
filter edits.

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
include `report_name`, `ref_doctype`, `report_type`, and `is_standard` \
in its `data`. Default to Report Builder unless the user's ask \
explicitly needs raw SQL (Query Report) or Python (Script Report). \
is_standard='Yes' requires developer_mode + Administrator at save - do \
not set it unless you know the caller has both. For Query Report, a \
non-empty `query` field is required and must pass check_safe_sql_query \
(single SELECT, no DDL / DML / multi-statement). For Script Report, a \
non-empty `report_script` is required and the caller must have the \
Script Manager role.
""".strip(),

	"create_dashboard": """
Your current task is to CREATE A NEW DASHBOARD. A Dashboard is a \
LAYOUT container - it does not aggregate on its own. Emit the Dashboard \
doc with `dashboard_name`, `is_default` (0 unless the user wants this \
to REPLACE whatever the site-wide default is - setting is_default=1 \
auto-clears is_default on every other Dashboard), `is_standard` (0 for \
site-local; 1 forces every referenced chart / card to also be \
standard), and `charts` (list of Dashboard Chart names to render). If \
the user asked for charts / cards that don't exist yet, emit the \
Dashboard Chart / Number Card items BEFORE the Dashboard so link \
references resolve on apply.
""".strip(),

	"create_dashboard_chart": """
Your current task is to CREATE A NEW DASHBOARD CHART. Dashboard Chart \
has TWO separate type fields - do not confuse them. `chart_type` is \
the AGGREGATION MODE (Count / Sum / Average / Group By / Custom / \
Report) and is set_only_once (cannot change after first save). `type` \
is the RENDER SHAPE (Line / Bar / Percentage / Pie / Donut / Heatmap). \
Pick chart_type from what the user wants to MEASURE:\n\
  - "count of invoices this month" -> chart_type=Count + document_type \
+ based_on (date field) + timeseries=1.\n\
  - "total sales by month" -> chart_type=Sum + document_type + \
based_on + value_based_on (numeric field).\n\
  - "customers by territory" -> chart_type=Group By + document_type + \
group_by_based_on + group_by_type (Count / Sum / Average) + \
aggregate_function_based_on (if Sum / Average).\n\
  - "chart of the Overdue Invoices report" -> chart_type=Report + \
report_name + either use_report_chart=1 or x_field + y_axis.\n\
Pick `type` (render shape) separately from the data shape: Line / Bar \
for time-series, Pie / Donut / Percentage for few-category \
distributions, Heatmap for daily density.
""".strip(),

	"create_number_card": """
Your current task is to CREATE A NEW NUMBER CARD. Number Card has a \
`type` field that picks the SOURCE: Document Type / Report / Custom. \
Each source uses a different subset of fields:\n\
  - type=Document Type: emit document_type + function (Count / Sum / \
Average / Minimum / Maximum) + aggregate_function_based_on when \
function is not Count. If document_type is a child table, also emit \
parent_document_type.\n\
  - type=Report: emit report_name + report_field + report_function \
(Sum / Average / Minimum / Maximum - Count is NOT valid here; use \
type=Document Type with function=Count instead).\n\
  - type=Custom: emit method (dotted path to a whitelisted Python \
function returning {value, fieldtype, route?}) AND filters_config (the \
filter UI JSON exposed to dashboard users of this card).\n\
filters_json is a JSON STRING of Frappe filter triples (e.g. \
'[[\"Sales Invoice\",\"status\",\"=\",\"Unpaid\"]]'), not a Python \
dict. '[]' means 'across all rows' - if the user said anything \
implying a filter, that's a blocker, not a default. Use \
dynamic_filters_json for render-time filters (current user, this \
month, today) that should not freeze at save.
""".strip(),

	"create_auto_email_report": """
Your current task is to CREATE AN AUTO EMAIL REPORT that emails an \
existing Report on a schedule. Emit report (the Report to run), \
email_to (one recipient per line), frequency (Daily / Weekdays / \
Weekly / Monthly), format (HTML inline, or CSV / XLSX / PDF \
attachment), filters (JSON string of filter values matching the \
Report's declared filters), and send_if_data (default 1 - SILENTLY \
SUPPRESSES the email when the filtered report has zero rows). For \
rolling-window reports ('last 7 days', 'last month'), combine \
from_date_field (the Report's date column name) with \
dynamic_date_filters_based_on (Daily / Weekly / Monthly / Yearly) so \
the scheduler auto-fills the date range. Remember: the scheduler must \
be running for Auto Email Reports to fire; Script Reports need roles \
the scheduler worker holds.
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
	"create_auto_email_report": (
		"Generate an Auto Email Report changeset item that delivers an "
		"existing Report on the named frequency in the named format, "
		"with filters (and optional rolling-date window) scoped to "
		"produce a meaningful recurring payload."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_report": "Frappe Developer - Reports Specialist (Report)",
	"create_dashboard": "Frappe Developer - Reports Specialist (Dashboard)",
	"create_dashboard_chart": "Frappe Developer - Reports Specialist (Dashboard Chart)",
	"create_number_card": "Frappe Developer - Reports Specialist (Number Card)",
	"create_auto_email_report": "Frappe Developer - Reports Specialist (Auto Email Report)",
}

_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"


def _wrap_module_context(snippet: str) -> str:
	return (
		f"{_MODULE_CONTEXT_MARKER} (ERPNext domain knowledge - respect these "
		"alongside the shape-defining fields above):\n"
		"The snippet may contain layered sections labeled PRIMARY FAMILY "
		"(cross-module invariants shared across a family like Transactions "
		"or Operations), PRIMARY MODULE (the specific ERPNext module's "
		"conventions), and SECONDARY MODULE CONTEXT (advisory context from "
		"related modules). Treat every labeled section as authoritative. "
		"If a FAMILY-level invariant conflicts with a shape-defining "
		"default above, the FAMILY invariant wins - families encode real "
		"controller-enforced rules.\n\n"
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
