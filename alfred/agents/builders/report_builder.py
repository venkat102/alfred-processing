"""Report Builder specialist - a Report-DocType-specialized variant of the Developer agent.

Selected by ``build_alfred_crew`` when ``intent == "create_report"`` and
``ALFRED_PER_INTENT_BUILDERS=1``. Mirrors the DocType Builder shape: the
specialist contributes a Report-focused Agent and a prompt enhancer that
appends a registry-driven checklist to the base ``generate_changeset``
template.

Spec: ``docs/specs/2026-04-22-insights-to-report-handoff.md``.
"""

from __future__ import annotations

from crewai import Agent

from alfred.registry.loader import IntentRegistry

_REPORT_BACKSTORY = """
You are a Frappe Report specialist. You know the three Report types - \
Report Builder (field-list + filters, no code; the safe default), Query \
Report (raw SQL with parameter interpolation; use when aggregations exceed \
Report Builder's vocabulary), and Script Report (Python-driven; powerful \
but sandboxing-heavy, V2+ only). You know Reports anchor to a single \
``ref_doctype`` (the source) and carry columns (field + label), filters \
(field + operator + value), and sort (field + direction). You know \
``is_standard`` governs whether the Report lives in an app's filesystem \
(module + file write) or stays site-local (DB-only); default non-standard \
unless the user is shipping an app. You know that Reports bind to a Module \
for navigation and access control, and that the Report's permission scope \
is governed by the ref_doctype's perms unless explicitly overridden. Every \
Report you emit MUST include ``ref_doctype``, ``report_type``, \
``is_standard``, and ``module`` in its ``data``. If the user did not \
specify a value, use the registry default and record which fields were \
defaulted in ``field_defaults_meta``.
""".strip()

_CHECKLIST_MARKER = "SHAPE-DEFINING FIELDS for create_report"
_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"


def render_registry_checklist(schema: dict) -> str:
	"""Render the Report registry schema as a checklist for the agent prompt."""
	lines = [
		f"{_CHECKLIST_MARKER} (you MUST include every one of these in `data`):",
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
		'"report_type": {{"source": "default", "rationale": "..."}}, '
		'"ref_doctype": {{"source": "user"}}}}'
	)
	return "\n".join(lines)


def _wrap_module_context(snippet: str) -> str:
	return (
		f"{_MODULE_CONTEXT_MARKER} (target-module conventions - respect these "
		"alongside the shape-defining fields above):\n"
		f"{snippet}"
	)


def build_report_builder_agent(site_config: dict, custom_tools: dict | None) -> Agent:
	"""Build a CrewAI Agent that is a Report specialist variant of the Developer."""
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
		role="Frappe Developer - Report Specialist",
		goal=(
			"Generate a production-ready Report changeset item whose `data` "
			"includes every shape-defining field from the registry, with "
			"`field_defaults_meta` describing which fields were defaulted."
		),
		backstory=_REPORT_BACKSTORY,
		allow_delegation=False,
		tools=tools,
		verbose=False,
	)


def enhance_generate_changeset_description(base: str, module_context: str = "") -> str:
	"""Return base description with Report checklist and optional module context.

	Idempotent per section: checklist appended once (guarded by
	_CHECKLIST_MARKER), module context appended once (guarded by
	_MODULE_CONTEXT_MARKER). Double-enhance is a no-op.
	"""
	out = base
	if _CHECKLIST_MARKER not in out:
		schema = IntentRegistry.load().get("create_report")
		out = out + "\n\n" + render_registry_checklist(schema)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
