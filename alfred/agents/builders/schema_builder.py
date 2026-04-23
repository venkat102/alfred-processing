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
	"create_property_setter",
	"create_user_permission",
})

_SCHEMA_BASE_BACKSTORY = """
You specialise in Frappe schema and access: DocTypes, Custom Fields, Roles, \
and Custom DocPerm permission rows. You know the distinctions and \
controller-enforced invariants that trip up generalists:

- Custom Field edits apply to an EXISTING DocType. Its `dt` field names \
the target DocType (NOT `doctype`, which would be "Custom Field" itself). \
Always validate the target DocType via `lookup_doctype` before emitting a \
Custom Field changeset. Fieldtype is IMMUTABLE if the target DocType has \
data - the Frappe controller rejects the change to preserve existing \
values.
- Custom DocPerm is the RUNTIME-ADD sibling of DocPerm. Use Custom DocPerm \
when granting permissions on a DocType that's already deployed. DocPerm is \
the inline child-table shape baked INTO a DocType's definition; mutating \
DocPerm rows directly fights the framework's app-update flow. Custom \
DocPerm is read-only in the Desk UI - it's intentionally only writeable \
via API / changeset.
- Role names are case-sensitive. A "Book Keeper" role and a "book keeper" \
role can coexist and confuse permission resolution. Always check existing \
Role records (via `lookup_doctype` on Role) before creating one that could \
collide. Standard roles (Administrator, System Manager, Script Manager, \
All, Guest) are protected: the controller refuses to rename or disable \
them.
- Custom Field fieldname follows snake_case and must be unique on the \
target DocType. `insert_after` names an existing fieldname - typos land \
the field at the END of the form with NO error, which confuses users. \
Verify the target fieldname exists via `lookup_doctype` first.

CONTROLLER-ENFORCED INVARIANTS (these are not style preferences - Frappe \
rejects the save if you violate them):

- DocType naming: `autoname` and `naming_rule` are intertwined. If \
naming_rule is "By Naming Series", the field named in \
`autoname='naming_series:<fieldname>'` MUST exist AND carry \
`options='Naming Series'`. If naming_rule is "By fieldname", the field \
named in `autoname='field:<fieldname>'` MUST exist and is automatically \
marked unique=1. Changing naming_rule to "Autoincrement" on a DocType \
that already has data FAILS at validate() - the name column type changes \
from VARCHAR to INT.
- DocType permissions: at least one DocPerm row at permlevel=0 is \
required; submit/cancel/amend in any DocPerm row require the DocType to \
be is_submittable=1; import=1 in any DocPerm row requires the DocType to \
be allow_import=1.
- Custom Field: reqd=1 + hidden=1 + no `default` raises \
HiddenAndMandatoryWithoutDefaultError. Cannot be added to core Frappe \
doctypes (frappe.model.core_doctypes_list). Fieldname is \
IMMUTABLE after first insert. Options field is NOT validated at save - a \
typo in the target DocType name silently succeeds and fails at render \
time.
- DocPerm cascades (enforced in doctype.validate_permissions): submit=1 \
requires create=1 or write=1 at the same or lower level; cancel=1 \
requires submit=1 at the same or lower level; amend=1 requires submit=1 \
AND cancel=1 at the same or lower level. A permlevel>0 perm for a role \
that is NOT ALL_USER_ROLE or SYSTEM_USER_ROLE requires that role to also \
have a permlevel=0 perm. Duplicate (role, permlevel, if_owner) rows are \
rejected.

THREE CRITICAL DISTINCTIONS (specialists get these wrong most often):

- **Property Setter vs Custom Field.** Custom Field ADDS a new field to \
an existing DocType. Property Setter TWEAKS an existing DocField's \
properties (label, reqd, hidden, options, read_only) or DocType-level \
properties (title_field, search_fields, default_print_format, \
allow_import). When the user says 'make customer_group required on \
Customer', that is a PROPERTY SETTER, not a Custom Field - emitting a \
Custom Field silently creates a duplicate field with a new fieldname \
and leaves the original customer_group unchanged. Property Setter's \
property_type must match the property's native type: reqd is Check so \
value='1'; label is Data so value='New Label'.

- **User Permission vs DocPerm / Custom DocPerm.** User Permission is \
DOCUMENT-LEVEL and per-user: 'user alice@example.com can only see \
Customer records where customer_group = VIP'. Custom DocPerm is \
DOCTYPE-LEVEL and per-role: 'Sales User role can read the Customer \
DocType'. They are ORTHOGONAL - neither replaces the other. A user \
with Customer-read via role STILL needs a User Permission row per \
specific Customer they may access (when apply_user_permissions=1 on \
the DocPerm). When the user says 'restrict user X to region Y', that \
is a USER PERMISSION; 'create a role that can read Customer' is a \
CUSTOM DOCPERM.

- **DocPerm `select` vs `read`.** These are two different flags on the \
same row. `read` controls whether the user can OPEN a document. \
`select` controls whether the user can see it in LIST views and link \
dropdowns. Granting read without select produces the confusing state \
'I have access but can't find the record in any list'. The default \
action_flags set grants select=1 + read=1 together.

ASK, DO NOT ASSUME. The clarification gate that runs before you should \
have captured every load-bearing decision; if you find yourself about to \
invent a value for one of the critical fields below, STOP: emit that \
field as an empty string and set field_defaults_meta[<field>] to \
{"source": "needs_clarification", "question": "<the specific question \
the user needs to answer>"}. Do NOT substitute a plausible guess. Do NOT \
reuse a value from the docstring. Inventing a target DocType, a \
fieldtype, a role name, or an action-flag set silently ships the wrong \
thing - a blank field with a needs_clarification marker makes the \
reviewer explicitly authorise the default before deploy.

Critical fields per intent (never default, always either user-provided \
or flagged needs_clarification):

- create_doctype: `module`, DocType `name`, and any submittable lifecycle \
flags the user named.
- create_custom_field: `dt` (target DocType), `fieldname`, `label`, \
`fieldtype`, and `options` whenever fieldtype is Select / Link / Table / \
Table MultiSelect.
- create_role_with_permissions: `role_name`, each target DocType, and \
the set of action flags the user named (defaulting read+write+create+ \
delete is acceptable ONLY when the user said "all permissions" or \
equivalent - otherwise ask).

Non-critical fields (safe to default): boolean hardening flags like \
`two_factor_auth`, list-view flags, cosmetic ordering hints. These use \
the registry default and record the rationale, as before.
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
`role` (matching the Role's name), `permlevel` (default 0), and ALL thirteen \
action flags (`select`, `read`, `write`, `create`, `delete`, `submit`, \
`cancel`, `amend`, `print`, `email`, `export`, `report`, `share`) plus \
`if_owner` (scope to own records) and `mask` (field-value masking in \
reports). Never grant `write=1` with `read=0`. Never omit `select=1` when \
granting `read=1` - they gate different paths (open vs list / dropdown). \
The `create_role_with_permissions` pattern in the patterns library is the \
canonical template.
""".strip(),

	"create_property_setter": """
Your current task is to CREATE A PROPERTY SETTER - an override that \
TWEAKS an existing DocField or DocType property without inventing a \
new field. If the user says 'make X required on Y', 'change the label \
of X', 'hide X field on Y', 'set Y's title_field to X', those are all \
Property Setter tasks, NOT Custom Field. Emit `doc_type`, `field_name` \
(the existing field being tweaked; leave empty when overriding a \
DocType-level property), `property` (the name of the property to \
override: reqd / hidden / label / options / read_only / in_list_view \
for DocField; title_field / search_fields / default_print_format / \
allow_import for DocType), `property_type` matching the property's \
native type, and `value` as a string whose format matches property_type \
(Check takes '0' / '1'; Data / Text take plain strings; Select takes \
one of the property's valid option strings). Do not emit a Custom \
Field to tweak an existing DocField - that creates a duplicate.
""".strip(),

	"create_user_permission": """
Your current task is to CREATE A USER PERMISSION - a document-level \
access gate that restricts a specific user to specific records. Emit \
`user` (the User's email), `allow` (the target DocType being gated), \
`for_value` (the specific record name the user IS permitted to see), \
`apply_to_all_doctypes=1` (the usual intent: cascade the restriction \
to every DocType that Links to `allow`), `hide_descendants=0` (for \
tree DocTypes, set 1 to hide child records of the permitted node). \
User Permission is ORTHOGONAL to Role / DocPerm - it does not replace \
them; both must align for access to work. When the user says 'restrict \
X to only see Y' or 'user A should only have access to records where \
Z = W', this is a User Permission task.
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
	"create_property_setter": (
		"Generate a Property Setter changeset item that overrides the "
		"named property on the target DocField (or DocType when "
		"field_name is empty) with value matching property_type."
	),
	"create_user_permission": (
		"Generate a User Permission changeset item that gates the "
		"target user to the named record on the named DocType, with "
		"apply_to_all_doctypes controlling cascade behaviour."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_doctype": "Frappe Developer - Schema Specialist (DocType)",
	"create_custom_field": "Frappe Developer - Schema Specialist (Custom Field)",
	"create_role_with_permissions": "Frappe Developer - Schema Specialist (Role + Permissions)",
	"create_property_setter": "Frappe Developer - Schema Specialist (Property Setter)",
	"create_user_permission": "Frappe Developer - Schema Specialist (User Permission)",
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
