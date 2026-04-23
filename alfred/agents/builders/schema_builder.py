"""Schema & Access specialist family - builders for Frappe schema and
access-control primitives.

Selected by ``build_alfred_crew`` when ``intent`` is one of the Schema
family intents and ``ALFRED_PER_INTENT_BUILDERS=1``. The family covers:

  - ``create_doctype`` - a new DocType.
  - ``create_custom_field`` - a Custom Field on an existing DocType.
  - ``create_role_with_permissions`` - a Role plus one or more Custom
    DocPerm rows granting it access on target DocTypes.

Shared family backstory teaches the model the distinctions that trip
up the generic Developer: DocType vs Custom Field (dt field on
Custom Field names the TARGET DocType, not the Custom Field row
itself), DocPerm vs Custom DocPerm (Custom DocPerm is the runtime-add
sibling used when granting permissions on an already-deployed DocType),
Role name collisions (case-sensitive, check via lookup_doctype before
creating).

Registry: ``alfred/registry/intents/create_<intent>.json`` per intent.

Pairs with V2 module specialist: if the prompt also names an ERPNext
module (Accounts, HR, Stock, ...), the module context snippet is
grafted into the description alongside the checklist.
"""

from __future__ import annotations

from crewai import Agent

from alfred.registry.loader import IntentRegistry

SCHEMA_INTENTS: frozenset[str] = frozenset({
	"create_doctype",
	"create_custom_field",
	"create_role_with_permissions",
})

_SCHEMA_BASE_BACKSTORY = """
You specialise in Frappe schema and access: DocTypes, Custom Fields, Roles, \
and Custom DocPerm permission rows. You know the distinctions that trip up \
generalists:

- Custom Field edits apply to an EXISTING DocType. Its `dt` field names \
the target DocType (NOT `doctype`, which would be "Custom Field" itself). \
Always validate the target DocType via `lookup_doctype` before emitting a \
Custom Field changeset.
- Custom DocPerm is the RUNTIME-ADD sibling of DocPerm. Use Custom DocPerm \
when granting permissions on a DocType that's already deployed. DocPerm is \
the inline child-table shape baked INTO a DocType's definition; mutating \
DocPerm rows directly fights the framework's app-update flow.
- Role names are case-sensitive. A "Book Keeper" role and a "book keeper" \
role can coexist and confuse permission resolution. Always check existing \
Role records (via `lookup_doctype` on Role) before creating one that could \
collide.
- Custom Field fieldname follows snake_case and must be unique on the \
target DocType. `insert_after` places it after an existing field; without \
it, the field lands at the bottom of the form which confuses users.
""".strip()

_INTENT_FRAGMENTS: dict[str, str] = {
	"create_doctype": """
Your current task is to CREATE A NEW DOCTYPE. Every DocType you emit MUST \
include `module`, `is_submittable`, `autoname`, `istable`, `issingle`, and \
at least one `permissions` row in its `data`. Submittable documents have a \
draft/submitted/cancelled lifecycle; most DocTypes are not submittable. \
Prefer `autoincrement` naming unless the user wants meaningful IDs. If the \
user did not specify a value, use the registry default and record which \
fields were defaulted in `field_defaults_meta`.
""".strip(),

	"create_custom_field": """
Your current task is to ADD A CUSTOM FIELD to an existing DocType. The `dt` \
field names the TARGET DocType (the one gaining the new field). The `fieldname` \
must be snake_case and unique on the target. Call `lookup_doctype` on the \
target first to verify the DocType exists and to pick a sensible \
`insert_after` field. The `fieldtype` determines downstream behaviour: \
Link fields need `options` (the linked DocType), Select fields need \
`options` (newline-separated values). The `custom_field_on_existing_doctype` \
pattern in the patterns library carries the canonical template.
""".strip(),

	"create_role_with_permissions": """
Your current task is to CREATE A NEW ROLE AND GRANT IT PERMISSIONS on one or \
more existing DocTypes. Emit ONE `Role` changeset item followed by ONE \
`Custom DocPerm` item per (target DocType, permlevel) combination. The Role \
item carries `role_name`, `desk_access=1` (unless the prompt mentions a \
portal-only role), `two_factor_auth=0`. Each Custom DocPerm item carries \
`parent` (the target DocType), `parenttype="DocType"`, `parentfield="permissions"`, \
`role` (matching the Role's name), `permlevel` (default 0), and the action \
flags (`read`, `write`, `create`, `delete`, `submit`, `cancel`, `amend`, \
`print`, `email`, `export`, `report`, `share`). Never grant `write=1` with \
`read=0`. The `create_role_with_permissions` pattern in the patterns \
library is the canonical template.
""".strip(),
}

_INTENT_GOALS: dict[str, str] = {
	"create_doctype": (
		"Generate a production-ready DocType changeset item whose `data` "
		"includes every shape-defining field from the registry, with "
		"`field_defaults_meta` describing which fields were defaulted."
	),
	"create_custom_field": (
		"Generate a production-ready Custom Field changeset item whose `data` "
		"includes `dt`, `fieldname`, `label`, `fieldtype`, and type-specific "
		"`options` where required, with `field_defaults_meta` describing which "
		"fields were defaulted."
	),
	"create_role_with_permissions": (
		"Generate a multi-item changeset: one Role changeset item plus one "
		"Custom DocPerm item per target DocType, with consistent role_name "
		"across them and sensible action-flag defaults."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_doctype": "Frappe Developer - Schema Specialist (DocType)",
	"create_custom_field": "Frappe Developer - Schema Specialist (Custom Field)",
	"create_role_with_permissions": "Frappe Developer - Schema Specialist (Role + Permissions)",
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
	"""Render an intent registry schema as a checklist for the agent prompt.

	Shared helper - the same shape used by every family builder. The
	marker is intent-specific so multiple family files can append to
	the same base without clobbering each other (rare but possible
	when a multi-intent changeset flows through).
	"""
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
	return _SCHEMA_BASE_BACKSTORY + "\n\n" + _INTENT_FRAGMENTS[intent]


def build_schema_agent(
	intent: str,
	site_config: dict,
	custom_tools: dict | None,
) -> Agent:
	"""Build a CrewAI Agent specialised for a Schema family intent.

	Intent must be one of ``SCHEMA_INTENTS``. Slots into the same
	position as the generic Developer in ``build_alfred_crew``. Role
	and goal carry an intent-specific suffix so logs and tool scopes
	still identify which specialist fired. Tools come from
	``custom_tools`` - the same MCP tool map the generic Developer
	uses, restricted to the read-only lookup set.
	"""
	if intent not in SCHEMA_INTENTS:
		raise ValueError(
			f"build_schema_agent: intent {intent!r} is not in SCHEMA_INTENTS"
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
	"""Return the base generate_changeset description with intent checklist
	and optional module context appended.

	Idempotent per section: the intent checklist is appended once
	(guarded by the intent-specific marker), and the module context
	is appended once (guarded by the shared MODULE CONTEXT marker).
	Double-enhance is a no-op.
	"""
	if intent not in SCHEMA_INTENTS:
		raise ValueError(
			f"enhance_generate_changeset_description: intent {intent!r} is "
			"not in SCHEMA_INTENTS"
		)

	out = base
	marker = _checklist_marker(intent)
	if marker not in out:
		schema = IntentRegistry.load().get(intent)
		out = out + "\n\n" + render_registry_checklist(schema, intent)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
