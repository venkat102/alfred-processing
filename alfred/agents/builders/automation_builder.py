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
Notifications, and Workflows. Four quirks are load-bearing:

- Server Scripts run under RestrictedPython. NO `import` statements - \
the deploy dry-run rejects any script containing `import`. Use the \
pre-bound `frappe`, `frappe.utils`, `frappe.db`, `json`, and `datetime` \
directly. Never use `frappe.db.sql` with string interpolation (SQL \
injection); prefer `frappe.db.get_value` / `frappe.get_all` or \
parameterised queries.
- Notifications' `event` field is load-bearing. Use `New` to alert \
approvers BEFORE they act (their click is what fires Submit, so \
emailing them on Submit would email themselves). Use `Submit` for \
post-approval downstream notifications. Use `Days After` / `Days \
Before` for date-based reminders. The patterns library carries \
`approval_notification` (event=New) and `post_approval_notification` \
(event=Submit) with worked examples - call `lookup_pattern` first.
- Workflows require three linked docs: one `Workflow`, one `Workflow \
State` per state, and one `Workflow Action Master` per distinct action \
name. Frappe auto-creates a `workflow_state` Custom Field on the \
target DocType on first save - you do NOT need to emit that Custom \
Field yourself. Transitions name states + actions + allowed_roles; \
unreachable states or missing allowed_roles fail silently at runtime.
- Client Scripts run in the browser sandbox. Use `frm.set_value`, \
`frm.add_custom_button`, `frm.refresh_field`, NOT jQuery selectors \
against Desk chrome (Desk HTML is not a stable contract). `frm.doc` \
reads the current form values, not the persisted DB state.

For every automation task, call `lookup_pattern` first to see if a \
curated idiom exists in the patterns library. Adapting a pattern is \
always safer than hand-rolling from scratch.
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
		"changeset item. For each field above, record whether the value came "
		"from the user or from the registry default, and include the registry "
		"rationale when defaulted. Example (doubled braces because this prompt "
		"is interpolated by str.format):"
	)
	lines.append(
		'  "field_defaults_meta": {{'
		'"<defaulted_field>": {{"source": "default", "rationale": "..."}}, '
		'"<user_field>": {{"source": "user"}}}}'
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
