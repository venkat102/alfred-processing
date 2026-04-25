"""Automation & Behavior specialist family - builders for Frappe
event-driven primitives.

Selected by ``build_alfred_crew`` when ``intent`` is in
``AUTOMATION_INTENTS`` and ``ALFRED_PER_INTENT_BUILDERS=1``. The family
covers:

  - ``create_server_script`` - RestrictedPython hook firing on DocType
    events, API calls, scheduler ticks, or permission queries.
  - ``create_client_script`` - browser-side JS bound to a DocType's
    form / list / report view.
  - ``create_notification`` - event-driven alerts via Email / SMS /
    System Notification / Slack.
  - ``create_workflow`` - multi-state approval flow with allowed-role
    transitions, persisted as Workflow + Workflow State rows + Workflow
    Action Master rows.

This is the highest-value specialist family in the stack: the quirks
here (RestrictedPython bans imports, Notification event choice,
Workflow's auto-added workflow_state Custom Field, Client Script
sandbox) are exactly where the generic Developer drifts. The family
backstory front-loads those quirks so the agent stops relearning them
per prompt.
"""

from __future__ import annotations

from crewai import Agent

from alfred.agents.definitions import _resolve_llm_for_tier
from alfred.registry.loader import IntentRegistry

AUTOMATION_INTENTS: frozenset[str] = frozenset({
	"create_server_script",
	"create_client_script",
	"create_notification",
	"create_workflow",
	"create_webhook",
	"create_auto_repeat",
	"create_assignment_rule",
})

_AUTOMATION_BASE_BACKSTORY = """
You specialise in Frappe automation: Server Scripts, Client Scripts, \
Notifications, and Workflows. The controller-enforced contracts that \
trip up generalists:

- Server Scripts run under RestrictedPython. The FrappeTransformer \
compiles the script at save time and REJECTS any `import` / \
`__import__` / bare `exec` / assignment to `__builtins__` / attribute \
access that fails safer_getattr. Use the pre-bound `frappe`, `_`, \
`db`, `qb`, `frappe.utils.*`, `json`, `datetime` directly - no \
imports. Read doc state with `doc.fieldname` or \
`frappe.db.get_value`; mutate with `doc.db_set` for post-save fields. \
Never string-interpolate into `frappe.db.sql` (SQL injection); use \
parameterised queries or the ORM.
- Server Script script_type has FIVE values: DocType Event (needs \
reference_doctype + doctype_event), API (needs api_method + optional \
allow_guest + rate limit), Scheduler Event (needs event_frequency + \
cron_format when frequency=Cron; updates auto-sync a Scheduled Job \
Type), Permission Query (needs reference_doctype), Workflow Task. \
Frappe does NOT server-side-validate that the right companion field \
is present - the UI depends_on gates but if you emit a changeset with \
script_type=DocType Event and no reference_doctype, the script saves \
and silently never fires. Always emit the companion field.
- Server Script doctype_event has TWENTY-FOUR values not five. Pick \
carefully: Before Save runs on every save (use to throw and cancel). \
Before Submit runs only on 0->1 docstatus transition (use to gate \
approvals). After Insert for first-save-only side effects. Before \
Print for print-context derivations. The submitted-document variants \
(Before Save / After Save (Submitted Document)) run only when \
allow_on_submit fields change on an already-submitted doc.
- Notifications' `event` field is load-bearing. Use `New` to alert \
approvers BEFORE they act (their click is what fires Submit, so \
emailing them on Submit would email themselves their own approval). \
Use `Submit` for post-approval downstream notifications. Value \
Change requires value_changed naming the watched field. Days After / \
Days Before require date_changed (a Date / Datetime field) AND fire \
from the daily scheduler. Minutes After / Minutes Before require \
datetime_changed AND minutes_offset >= 10 (the scheduler tick is \
coarser than a few minutes - the controller rejects offsets below \
10). Method event requires `method` naming a function on the doc.
- Notifications' condition_type toggles which companion field \
applies: Python uses the `condition` field (frappe.safe_eval'd), \
Filters uses the `filters` field (evaluate_filters). Switching \
condition_type clears the OTHER field on save. channel is \
set_only_once; swapping Email<->Slack after first save fails. At \
least one recipient row is REQUIRED unless send_to_all_assignees=1 OR \
channel=Slack (which uses slack_webhook_url instead).
- Workflows auto-create the `workflow_state` Custom Field (hidden=1, \
Link to Workflow State, allow_on_submit=1, no_copy=1) on the target \
DocType on first save. DO NOT emit a separate Custom Field item for \
workflow_state in the changeset - the Workflow's on_update hook \
handles it. Setting is_active=1 auto-deactivates every other workflow \
on the same document_type (only ONE active workflow per DocType). \
Transitions must respect docstatus: transitions cannot originate \
from doc_status=2 (Cancelled) states; cannot move from doc_status=1 \
(Submitted) -> doc_status=0 (Draft); cannot jump from 0 -> 2 without \
an intermediate submitted state.
- Workflow state rows and transition rows use DIFFERENT role fields: \
`allow_edit` on a state row is the role that can EDIT a doc while \
it's in that state. `allowed` on a transition row is the role that \
can PERFORM the action to transition. Same DocType, different \
semantics.
- Client Scripts run in the browser sandbox. `view` is set_only_once \
(Form / List cannot toggle after save). Child tables (istable=1) \
cannot be targeted directly - create a Client Script on each parent \
DocType instead. Use `frappe.ui.form.on('<DocType>', { handler: ... })` \
for Form; `frappe.listview_settings['<DocType>'] = { ... }` for List. \
Common handlers: refresh, validate, <fieldname> (on-change), \
before_submit, after_save. Read via `frm.doc.<field>`; mutate via \
`frm.set_value`. NEVER jQuery against Desk chrome - Desk HTML is not \
a stable contract.

For every automation task, call `lookup_pattern` first to see if a \
curated idiom exists in the patterns library \
(approval_notification, post_approval_notification, \
validation_server_script, audit_log_server_script). Adapting a \
vetted pattern is always safer than hand-rolling from scratch.

THREE NEW PRIMITIVES distinguish from their look-alikes:

- **Webhook vs Server Script API.** Webhook is OUTBOUND (Frappe -> \
external URL) triggered by a document event. Server Script \
script_type=API is INBOUND (external caller -> Frappe endpoint). \
These are often conflated by users and by generalists. Webhook fires \
as a background job per event; a Server Script API is a synchronous \
HTTP handler. For "when X is submitted, POST to Y", the answer is \
Webhook, not Server Script API. Webhooks support HMAC signing via \
enable_security + webhook_secret - enable for any endpoint outside \
your network.
- **Auto Repeat creates COPIES, not templates.** When the scheduler \
fires, Frappe clones the reference_document AS-IS at that moment - \
any changes you make to the source document between generations are \
reflected in new copies. If the user wants the generated doc to \
stay fixed to the setup-time shape, they must freeze the template \
(lock edits, set allow_on_submit fields deliberately). Monthly \
repeats on the same day-of-month as start_date - a Jan 31 start \
SKIPS Feb 28, Apr 30, etc. unless repeat_on_last_day=1.
- **Assignment Rule vs Workflow.** Assignment Rule ROUTES documents \
to users (creates ToDos). Workflow TRACKS state on documents \
(docstatus transitions, allowed_roles per transition). They are \
complementary - a Workflow can drive state while Assignment Rules \
route to different roles per state. Assignment strategies: Round \
Robin cycles users; Load Balancing picks the user with the fewest \
open assignments; Based on Field routes by a User field on the doc. \
assign_condition is REQUIRED - the rule fires only when the \
condition is truthy, so an empty condition means the rule never \
fires.

ASK, DO NOT ASSUME. The clarification gate that runs before you \
should have captured every load-bearing decision; if you find \
yourself about to invent a value for one of the critical fields \
below, STOP: emit that field as an empty string and set \
field_defaults_meta[<field>] to {"source": "needs_clarification", \
"question": "<the specific question the user needs to answer>"}. Do \
NOT substitute a plausible guess. Do NOT reuse a value from the \
docstring. Automation primitives are the easiest place in Frappe to \
do the wrong thing silently - an inferred event type, an invented \
condition, or a fabricated allowed-roles list produces scripts that \
fire at the wrong moment or workflows that nobody can advance. A \
blank field with a needs_clarification marker makes the reviewer \
explicitly authorise the default before deploy.

Critical fields per intent (never default, always either user-provided \
or flagged needs_clarification):

- create_server_script: `reference_doctype` and `doctype_event` when \
script_type=DocType Event (Before Save vs On Submit vs After Save is \
load-bearing); the `script` body itself; `condition` if the script is \
conditional.
- create_client_script: `dt` (target DocType), the handler event names \
(refresh, validate, <fieldname> on change), and the logic body.
- create_notification: `document_type`, `event`, `recipients` (the \
specific field / role / email the user named), `subject`, and \
`condition` if the notification is conditional. NEVER guess `event` \
just to have a value - "notify the approver when the claim is \
submitted" means event=New (to tell the approver BEFORE they click), \
not event=Submit.
- create_workflow: `document_type`, full list of `states`, full list \
of `transitions` including `allowed_roles` per transition. An \
invented allowed_roles list means nobody can advance the workflow.

Non-critical fields (safe to default): `disabled=0`, `enabled=1`, \
`is_active=1`, `channel=Email`, `send_email_alert=0`, \
`override_status=0`. These use the registry default and record the \
rationale, as before.
""".strip()

_INTENT_FRAGMENTS: dict[str, str] = {
	"create_server_script": """
Your current task is to CREATE A NEW SERVER SCRIPT. Pick `script_type` \
based on the trigger: `DocType Event` for Save / Submit / Cancel hooks \
(most common); `API` for ad-hoc whitelisted endpoints; `Scheduler` for \
recurring jobs; `Permission Query` for row-level permission filters. \
For DocType Event, `reference_doctype` names the target and \
`doctype_event` names the hook point. The `script` body runs under \
RestrictedPython - no `import` statements. The \
`validation_server_script` and `audit_log_server_script` patterns \
cover the two most common shapes with vetted templates.
""".strip(),

	"create_client_script": """
Your current task is to CREATE A NEW CLIENT SCRIPT. `dt` names the \
target DocType. `view` is Form / List / Report - Form is the common \
case. The `script` body uses `frappe.ui.form.on("<DocType>", { ... })` \
with handler functions like `refresh`, `<fieldname>`, \
`validate`, `after_save`. Read current form state via `frm.doc.<field>`, \
mutate via `frm.set_value`, add buttons via `frm.add_custom_button`. \
Avoid jQuery selectors against Desk chrome.
""".strip(),

	"create_notification": """
Your current task is to CREATE A NEW NOTIFICATION. Pick `event` \
carefully: `New` alerts the approver before they act, `Submit` alerts \
the requester / downstream after approval, `Save` is almost never \
right (fires on every draft save). `channel` is Email (common) / SMS \
/ System Notification / Slack. `recipients` is a list of \
`receiver_by_document_field` (dynamic, names a link field on the \
target) OR static `{"role": "..."}` / `{"email": "..."}` entries. The \
`approval_notification` and `post_approval_notification` patterns \
carry the canonical event + recipient shapes.
""".strip(),

	"create_workflow": """
Your current task is to CREATE A NEW WORKFLOW. Emit ONE Workflow item \
plus one Workflow State item per distinct state and one Workflow Action \
Master item per distinct action name. Target DocTypes become \
submittable via a workflow that drives `docstatus` (Draft=0, \
Submitted=1, Cancelled=2 via `update_field: docstatus` + \
`update_value`). Do NOT emit the `workflow_state` Custom Field - \
Frappe auto-creates it. Transitions bind (state, action, next_state, \
allowed_roles); every role mentioned in allowed_roles must already \
exist on the site.
""".strip(),

	"create_webhook": """
Your current task is to CREATE A WEBHOOK - an outbound HTTP call \
triggered by a document event on webhook_doctype. Emit webhook_doctype, \
webhook_docevent (after_insert for first-insert-only, on_update for \
every save, on_submit / on_cancel / on_trash for lifecycle, \
workflow_transition for any Workflow state change), request_url, \
request_method (POST is the canonical choice), and request_structure \
(JSON or Form URL-Encoded - JSON is standard). For the payload emit \
EITHER webhook_data (JSON list of {fieldname, key} for flat 1:1 \
mapping) OR webhook_json (Jinja-templated raw JSON body for nested \
shapes) - never both. For any endpoint outside your network, enable \
enable_security=1 with webhook_secret; Frappe signs each request with \
HMAC-SHA256 via X-Frappe-Webhook-Signature. Webhook does NOT replace \
Server Script API - Webhook is OUTBOUND; Server Script API is \
INBOUND.
""".strip(),

	"create_auto_repeat": """
Your current task is to CREATE AN AUTO REPEAT schedule that clones an \
existing document on a frequency. Emit reference_doctype, \
reference_document (the template's name), frequency (Monthly common \
case), start_date, and end_date (empty for open-ended). Two gotchas \
worth calling out to the user: (a) generated copies reflect the \
template AS-IS AT FIRE TIME - mid-schedule edits to the template \
change future copies; (b) Monthly + start_date with day > 28 SKIPS \
short months unless repeat_on_last_day=1. For notification, emit \
notify_by_email=1 + recipients (one email per line) + optional \
Jinja subject / message. For submittable reference_doctypes, \
submit_on_creation=1 makes each copy auto-submit without manual \
review - enable only for fully automated flows.
""".strip(),

	"create_assignment_rule": """
Your current task is to CREATE AN ASSIGNMENT RULE that routes target \
documents to users via ToDos. Emit name, document_type, rule \
(strategy: Round Robin cycles users; Load Balancing picks the user \
with the fewest open assignments; Based on Field routes by a User \
field on the doc), users (the candidate pool), and CRITICALLY \
assign_condition (a Python expression evaluated on the doc - the \
rule fires ONLY when truthy; empty condition means the rule never \
fires, which the controller rejects). When rule='Based on Field', \
also emit field naming the Link-to-User field. Optional: \
unassign_condition (removes assignment when true), close_condition \
(closes the ToDo when true), due_date_based_on (Date field for the \
ToDo due date), assignment_days (skip users on time-off days), \
priority (lower numbers win when multiple rules match the same doc).
""".strip(),
}

_INTENT_GOALS: dict[str, str] = {
	"create_server_script": (
		"Generate a production-ready Server Script changeset item with a "
		"valid `script_type`, reference fields appropriate to the type, "
		"and a RestrictedPython body that contains no `import` "
		"statements."
	),
	"create_client_script": (
		"Generate a production-ready Client Script changeset item bound "
		"to the target DocType and view, using frm.* APIs rather than "
		"jQuery against Desk chrome."
	),
	"create_notification": (
		"Generate a production-ready Notification changeset item with an "
		"event choice matched to the business intent (New vs Submit vs "
		"date-based) and recipients that resolve either dynamically via "
		"document fields or statically via role / email."
	),
	"create_workflow": (
		"Generate a multi-item changeset: one Workflow, one Workflow "
		"State per distinct state, and one Workflow Action Master per "
		"distinct action name. Transitions must reach every state from "
		"the draft state; allowed_roles on each transition must name "
		"existing Roles."
	),
	"create_webhook": (
		"Generate a Webhook changeset item that fires an outbound "
		"HTTP request on the named document event, with a payload "
		"shape appropriate to request_structure (webhook_data for "
		"flat key/value, webhook_json for nested) and optional HMAC "
		"signing for external endpoints."
	),
	"create_auto_repeat": (
		"Generate an Auto Repeat changeset item that clones the "
		"reference document on the named frequency, with a date range "
		"that makes sense for the cadence and optional email "
		"notification on each generation."
	),
	"create_assignment_rule": (
		"Generate an Assignment Rule changeset item that routes "
		"matching documents to candidate users via the named strategy "
		"(Round Robin / Load Balancing / Based on Field), with a "
		"non-empty assign_condition."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_server_script": "Frappe Developer - Automation Specialist (Server Script)",
	"create_client_script": "Frappe Developer - Automation Specialist (Client Script)",
	"create_notification": "Frappe Developer - Automation Specialist (Notification)",
	"create_workflow": "Frappe Developer - Automation Specialist (Workflow)",
	"create_webhook": "Frappe Developer - Automation Specialist (Webhook)",
	"create_auto_repeat": "Frappe Developer - Automation Specialist (Auto Repeat)",
	"create_assignment_rule": "Frappe Developer - Automation Specialist (Assignment Rule)",
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
	return _AUTOMATION_BASE_BACKSTORY + "\n\n" + _INTENT_FRAGMENTS[intent]


def build_automation_agent(
	intent: str,
	site_config: dict,
	custom_tools: dict | None,
) -> Agent:
	"""Build a CrewAI Agent specialised for an Automation family intent."""
	if intent not in AUTOMATION_INTENTS:
		raise ValueError(
			f"build_automation_agent: intent {intent!r} is not in AUTOMATION_INTENTS"
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

	llm = _resolve_llm_for_tier(site_config or {}, tier="agent")

	return Agent(
		role=_INTENT_ROLES[intent],
		goal=_INTENT_GOALS[intent],
		backstory=_build_backstory(intent),
		llm=llm,
		allow_delegation=False,
		tools=tools,
		max_iter=2,
		max_retry_limit=1,
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
	if intent not in AUTOMATION_INTENTS:
		raise ValueError(
			f"enhance_generate_changeset_description: intent {intent!r} is "
			"not in AUTOMATION_INTENTS"
		)

	out = base
	marker = _checklist_marker(intent)
	if marker not in out:
		schema = IntentRegistry.load().get(intent)
		out = out + "\n\n" + render_registry_checklist(schema, intent)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
