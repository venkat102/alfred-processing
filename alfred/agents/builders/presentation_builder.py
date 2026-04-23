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
	"update_print_settings",
})

_PRESENTATION_BASE_BACKSTORY = """
You specialise in Frappe presentation: Print Formats, Letter Heads, \
Email Templates, and Web Forms. The controller-enforced contracts:

- Print Format has a SPLIT target. print_format_for=DocType requires \
doc_type (per-document layout, the common case). print_format_for=Report \
requires report AND auto-sets custom_format=1 at save (Report output \
cannot use the visual Builder). custom_format=1 toggles between \
hand-authored (Jinja html OR raw_commands for thermal / label \
printers) and visual Builder (drag-and-drop). raw_printing=1 is a \
third axis: when on, the system uses raw_commands instead of html \
(ESC/POS, ZPL for thermal / label printers). validate() enforces \
custom_format=1 + !raw_printing -> html required; custom_format=1 + \
raw_printing=1 -> raw_commands required; print_format_for=Report + \
no report link -> rejected. Jinja runs over the `doc` object ONLY - \
no frappe.session, no arbitrary frappe.* calls. Use \
doc.get_formatted('field') for currency / date formatting.
- Print Format is_standard='Yes' requires developer_mode AND the \
caller NOT being in migrate / install / test context. The controller \
rejects the save otherwise.
- Letter Head has TWO source fields: `source` for header (HTML or \
Image) and `footer_source` for footer (HTML or Image). When \
source='Image' and the image field is populated, set_image() \
AUTO-CONVERTS the attachment into <img> HTML and stores it in the \
content field at validate time - the conversion is one-way. is_default \
is a SITE-WIDE SINGLETON: setting is_default=1 auto-clears is_default \
on every other Letter Head. Deleting the default auto-promotes the \
next Letter Head saved. disabled=1 + is_default=1 is rejected as a \
contradictory combo.
- Email Template has a use_html TOGGLE that picks which body field \
applies: use_html=1 -> response_html (HTML Jinja, email_signature + \
email_footer from the sending EmailAccount auto-injected); \
use_html=0 -> response (rich text). Jinja context is ONLY the doc \
passed in - NO frappe.session, NO arbitrary frappe.* calls. Use \
frappe.utils.get_url() results pre-computed into the doc context, \
not direct function calls.
- Web Form web_form_fields is a STRICT WHITELIST. Only fields named \
there can be read, written, or submitted via the web form - other \
fields on the underlying DocType are IGNORED even when the submitter \
is an admin browsing the public route. No error is raised on \
unlisted-field submissions; the server silently drops them.
- Print Settings is a SINGLETON DocType (issingle=1). There is exactly \
ONE Print Settings record per site, and the intent is UPDATE, not \
create. The changeset item MUST use op='update' and target the \
document name 'Print Settings' (not a new identifier). Settings are \
site-wide defaults; individual Print Formats can override with_letterhead \
and letter_head on their own row. The pdf_generator choice is a \
site-wide trade-off: wkhtmltopdf is faster and more-tested but lacks \
modern CSS support; chrome renders flexbox / grid / web fonts but is \
slower and needs a heavier runtime dependency.
- Web Form has a LOGIN INTERLOCK: login_required=1 unlocks allow_edit, \
allow_multiple, allow_delete, allow_comments, allow_print, \
show_attachments, show_list. Setting those flags with login_required=0 \
silently has no effect. anonymous=1 is incompatible with \
login_required=1 - anonymous forms temporarily swap session.user to \
'Guest' during submit, bypassing owner-based perms. Link fieldtypes \
in web_form_fields auto-convert to Autocomplete at render time with \
options populated from the allowed DocType - this respects perms but \
can expose link options if apply_document_permissions=0. The `route` \
field is unique in the DB but NOT checked against existing website \
URLs; collisions with pages / blog posts fail silently at render.

ASK, DO NOT ASSUME. The clarification gate that runs before you \
should have captured every load-bearing decision; if you find \
yourself about to invent a value for one of the critical fields \
below, STOP: emit that field as an empty string and set \
field_defaults_meta[<field>] to {"source": "needs_clarification", \
"question": "<the specific question the user needs to answer>"}. Do \
NOT substitute a plausible guess. Do NOT reuse a value from the \
docstring. Presentation primitives touch user-visible pages and \
branded outputs - a fabricated `route` collides with an existing \
site URL; an invented `web_form_fields` list either leaks sensitive \
fields or hides the ones users need. A blank field with a \
needs_clarification marker makes the reviewer explicitly authorise \
the default before deploy.

Critical fields per intent (never default, always either user-provided \
or flagged needs_clarification):

- create_print_format: `doc_type`, the `html` body for Jinja \
templates (never invent placeholder layouts - ask what the user \
actually wants to print).
- create_letter_head: `content` and `footer` bodies - these carry \
branding; guessing legal disclaimers or contact details is worse \
than asking.
- create_email_template: `subject` and `response` bodies. Invented \
copy ends up on real outbound emails to real customers.
- create_web_form: `route` (must not collide), `login_required` when \
the user said anything about "public" vs "logged-in", the \
`web_form_fields` whitelist (never inherit all fields blindly).

Non-critical fields (safe to default): `standard="No"` for \
site-local, `default=0` on new print formats (don't replace the \
built-in default without asking), `use_html=1` on email templates, \
`allow_delete=0` on web forms. These use the registry default and \
record the rationale, as before.
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

	"update_print_settings": """
Your current task is to UPDATE PRINT SETTINGS - a SINGLETON Frappe \
DocType that holds site-wide print configuration. The changeset item \
MUST use op='update' and target the document name 'Print Settings' - \
this is NOT a create operation because issingle=1 means the site has \
exactly one Print Settings record already. Pick only the fields the \
user explicitly asked to change; leave every other field out of the \
changeset so their current values stay as-is. Key trade-offs: \
pdf_generator='wkhtmltopdf' is the default and the most-tested path; \
switch to 'chrome' only when a specific print format needs modern CSS \
(flexbox / grid / web fonts) and you've accepted the slower render + \
heavier runtime dependency. allow_print_for_draft=0 and \
allow_print_for_cancelled=0 are audit-discipline levers. raw_printing=1 \
requires a configured print_server - leave off unless the deployment \
has a network print server listening.
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
	"update_print_settings": (
		"Generate an UPDATE changeset item targeting the singleton "
		"'Print Settings' document with only the site-wide fields the "
		"user asked to change."
	),
}

_INTENT_ROLES: dict[str, str] = {
	"create_print_format": "Frappe Developer - Presentation Specialist (Print Format)",
	"create_letter_head": "Frappe Developer - Presentation Specialist (Letter Head)",
	"create_email_template": "Frappe Developer - Presentation Specialist (Email Template)",
	"create_web_form": "Frappe Developer - Presentation Specialist (Web Form)",
	"update_print_settings": "Frappe Developer - Presentation Specialist (Print Settings)",
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
