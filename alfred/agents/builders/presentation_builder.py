"""Presentation specialist family - builders for Frappe presentation
primitives.

Selected by ``build_alfred_crew`` when ``intent`` is in
``PRESENTATION_INTENTS`` and ``ALFRED_PER_INTENT_BUILDERS=1``. The
family covers:

  - ``create_print_format`` - Jinja-rendered or Builder-composed
    document layout for print / PDF output.
  - ``create_letter_head`` - HTML header + footer applied site-wide
    or per print format.
  - ``create_email_template`` - reusable Jinja subject + body for
    outbound emails (notification recipients, workflow alerts).
  - ``create_web_form`` - public-facing form that exposes a DocType
    at a URL route for unauthenticated or portal-user submission.

Presentation is the lowest-churn specialist family - print formats,
letter heads, and templates rarely go wrong once the shape is right.
Still worth a specialist because the Jinja context (doc vs
`frappe.session`) and the Web Form's field_list / route / permission
model are easy to get wrong and hard to test.
"""

from __future__ import annotations

from crewai import Agent

from alfred.registry.loader import IntentRegistry

PRESENTATION_INTENTS: frozenset[str] = frozenset({
	"create_print_format",
	"create_letter_head",
	"create_email_template",
	"create_web_form",
})

_PRESENTATION_BASE_BACKSTORY = """
You specialise in Frappe presentation: Print Formats, Letter Heads, \
Email Templates, and Web Forms. Four things to know:

- Print Formats come in two flavours: Jinja (you write the HTML with \
`{{ doc.fieldname }}` placeholders) and Builder (visual drag-and-drop, \
`raw_printing=0`). Jinja is the common case for custom invoice / \
receipt / quote layouts. Jinja renders over the `doc` context - \
reference the target DocType's field names directly.
- Letter Heads carry `content` (HTML header) and `footer` (HTML \
footer). Applied site-wide via `is_default=1`, or scoped per Print \
Format by setting the Print Format's `letter_head` field.
- Email Templates render `subject` + `response` (Jinja body) over the \
calling context. When called from a Notification, `doc` is the target \
document. Use `{{ frappe.utils.get_url() }}` for site URLs so the \
template survives domain changes.
- Web Forms expose a DocType publicly at `/<route>`. `login_required` \
governs authentication; `allow_multiple` lets one user submit more \
than once. `web_form_fields` is the explicit whitelist of fields the \
form exposes - it does NOT inherit all fields from the target \
DocType, and fields not listed stay hidden even to admins browsing \
the public route. Respect the underlying DocType's permlevel on \
read-back.
""".strip()

_INTENT_FRAGMENTS: dict[str, str] = {
	"create_print_format": """
Your current task is to CREATE A NEW PRINT FORMAT. `doc_type` names the \
target. `print_format_type` is Jinja (common) / Server / Builder. For \
Jinja, emit a complete `html` body that references `doc.<field>` \
values and uses `{%- -%}` for whitespace control. Set `standard="No"` \
unless shipping in an app. Set `default=1` only if this should become \
the default print format for the DocType (replaces the built-in one).
""".strip(),

	"create_letter_head": """
Your current task is to CREATE A NEW LETTER HEAD. `letter_head_name` \
is the identifier. `content` is the HTML header (logo, company name, \
contact block); `footer` is the HTML footer (page number, legal \
disclaimer). Set `is_default=1` only if this replaces the site-wide \
default - existing documents re-render with the new letter head on \
next print.
""".strip(),

	"create_email_template": """
Your current task is to CREATE A NEW EMAIL TEMPLATE. `name` is the \
identifier Notifications will reference. `subject` is a short Jinja \
string rendered over the calling context (usually `doc`). `response` \
is the HTML body; set `use_html=1` for HTML-mode templates. Keep the \
body short and reference doc fields via `{{ doc.<field> }}`.
""".strip(),

	"create_web_form": """
Your current task is to CREATE A NEW WEB FORM. `title` + `route` \
define the public URL (`/route`). `doc_type` names the backing \
DocType; fields listed in `web_form_fields` are exposed to the public \
form and others stay hidden. Set `login_required=1` for portal-gated \
forms, 0 for truly public. `allow_multiple=1` lets one user submit \
many rows; `allow_edit=1` lets them edit their own. Respect the \
target DocType's permlevel on fields - high-permlevel fields leak \
via a public form only if explicitly listed in web_form_fields.
""".strip(),
}

_INTENT_GOALS: dict[str, str] = {
	"create_print_format": (
		"Generate a production-ready Print Format changeset item bound "
		"to the target DocType, with a complete Jinja html body (or "
		"Builder layout) that references real fields on the target."
	),
	"create_letter_head": (
		"Generate a production-ready Letter Head changeset item with "
		"valid HTML in content / footer."
	),
	"create_email_template": (
		"Generate a production-ready Email Template changeset item "
		"with Jinja subject + response over the calling context."
	),
	"create_web_form": (
		"Generate a production-ready Web Form changeset item with an "
		"explicit web_form_fields whitelist, an appropriate "
		"login_required, and a route that doesn't collide with an "
		"existing site URL."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_print_format": "Frappe Developer - Presentation Specialist (Print Format)",
	"create_letter_head": "Frappe Developer - Presentation Specialist (Letter Head)",
	"create_email_template": "Frappe Developer - Presentation Specialist (Email Template)",
	"create_web_form": "Frappe Developer - Presentation Specialist (Web Form)",
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
	return _PRESENTATION_BASE_BACKSTORY + "\n\n" + _INTENT_FRAGMENTS[intent]


def build_presentation_agent(
	intent: str,
	site_config: dict,
	custom_tools: dict | None,
) -> Agent:
	"""Build a CrewAI Agent specialised for a Presentation family intent."""
	if intent not in PRESENTATION_INTENTS:
		raise ValueError(
			f"build_presentation_agent: intent {intent!r} is not in PRESENTATION_INTENTS"
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
	if intent not in PRESENTATION_INTENTS:
		raise ValueError(
			f"enhance_generate_changeset_description: intent {intent!r} is "
			"not in PRESENTATION_INTENTS"
		)

	out = base
	marker = _checklist_marker(intent)
	if marker not in out:
		schema = IntentRegistry.load().get(intent)
		out = out + "\n\n" + render_registry_checklist(schema, intent)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
