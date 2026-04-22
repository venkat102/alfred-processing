"""DocType Builder specialist - a DocType-specialized variant of the Developer agent.

Selected by ``build_alfred_crew`` when ``intent == "create_doctype"`` and
``ALFRED_PER_INTENT_BUILDERS=1``. The specialist contributes two things to
the existing ``generate_changeset`` pipeline:

  1. A specialized Agent with a DocType-focused backstory
     (``build_doctype_builder_agent``). The Agent slots into the same place
     the generic Developer occupies - same role family, same tools - so the
     rest of the crew (Tester, Deployer) works unchanged.

  2. A description enhancer (``enhance_generate_changeset_description``)
     that appends a registry-driven checklist to the base
     ``generate_changeset`` template. The template placeholders ({design},
     etc.) are preserved so ``crew.py``'s ``.format(**format_vars)``
     interpolation still works.

Registry: ``alfred/registry/intents/create_doctype.json``.
Spec: ``docs/specs/2026-04-21-doctype-builder-specialist.md``.
"""

from __future__ import annotations

from crewai import Agent

from alfred.agents.definitions import _resolve_llm_for_tier
from alfred.registry.loader import IntentRegistry

_DOCTYPE_BACKSTORY = """
You are a Frappe DocType specialist. You know the distinction between \
submittable documents (draft / submitted / cancelled lifecycle) and non-submittable \
documents; between autoincrement, field-based naming, format strings with series, \
prompt, and hash naming; between parent DocTypes, child tables, and singletons; and \
the minimum permission set required for a usable DocType. Every DocType you emit MUST \
include `module`, `is_submittable`, `autoname`, `istable`, `issingle`, and at least \
one `permissions` row in its `data`. If the user did not specify a value, use the \
registry default and record which fields were defaulted in `field_defaults_meta`.
""".strip()

_CHECKLIST_MARKER = "SHAPE-DEFINING FIELDS for create_doctype"
_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"


def _wrap_module_context(snippet: str) -> str:
	return (
		f"{_MODULE_CONTEXT_MARKER} (target-module conventions - respect these "
		"alongside the shape-defining fields above):\n"
		f"{snippet}"
	)


def render_registry_checklist(schema: dict) -> str:
	"""Render the DocType registry schema as a checklist for the agent prompt."""
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
		'"is_submittable": {{"source": "default", "rationale": "..."}}, '
		'"module": {{"source": "user"}}}}'
	)
	return "\n".join(lines)


def build_doctype_builder_agent(site_config: dict, custom_tools: dict | None) -> Agent:
	"""Build a CrewAI Agent that is a DocType specialist variant of the Developer.

	Slots into the same position as the generic Developer in
	``build_alfred_crew``. Role and goal stay close to the generic
	Developer so the crew pipeline (Tester, Deployer) treats its output
	identically. Only the backstory changes to inject DocType expertise.
	Tools come from ``custom_tools`` - the same MCP tool map the generic
	Developer uses.
	"""
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

	# Resolve the agent-tier LLM from site_config the same way build_agents
	# does for the generic Developer. Without this the CrewAI Agent would
	# fall back to its default (OpenAI) and fail with an auth error on a
	# site configured for Ollama.
	llm = _resolve_llm_for_tier(site_config or {}, tier="agent")

	return Agent(
		role="Frappe Developer - DocType Specialist",
		goal=(
			"Generate a production-ready DocType changeset item whose `data` "
			"includes every shape-defining field from the registry, with "
			"`field_defaults_meta` describing which fields were defaulted."
		),
		backstory=_DOCTYPE_BACKSTORY,
		llm=llm,
		allow_delegation=False,
		tools=tools,
		max_iter=2,
		max_retry_limit=1,
		verbose=False,
	)


def enhance_generate_changeset_description(base: str, module_context: str = "") -> str:
	"""Return the base generate_changeset description with intent checklist and optional module context appended.

	Idempotent per section: the intent checklist is appended once (guarded
	by _CHECKLIST_MARKER), and the module context is appended once
	(guarded by _MODULE_CONTEXT_MARKER). Double-enhance is a no-op.
	"""
	out = base
	if _CHECKLIST_MARKER not in out:
		schema = IntentRegistry.load().get("create_doctype")
		out = out + "\n\n" + render_registry_checklist(schema)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
