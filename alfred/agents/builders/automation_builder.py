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

from alfred.registry.loader import IntentRegistry

AUTOMATION_INTENTS: frozenset[str] = frozenset({
	"create_server_script",
	"create_client_script",
	"create_notification",
	"create_workflow",
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
}

_INTENT_ROLES: dict[str, str] = {
	"create_server_script": "Frappe Developer - Automation Specialist (Server Script)",
	"create_client_script": "Frappe Developer - Automation Specialist (Client Script)",
	"create_notification": "Frappe Developer - Automation Specialist (Notification)",
	"create_workflow": "Frappe Developer - Automation Specialist (Workflow)",
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
