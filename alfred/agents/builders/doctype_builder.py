"""Compatibility shim - DocType-specific builder APIs have moved into
``alfred.agents.builders.schema_builder`` as part of the Schema & Access
specialist family (which also covers Custom Field and Role + Permission
intents).

This module re-exports the old function names so existing callers
(crew.py dispatcher branches, test imports) keep working without edit.
New code should import ``build_schema_agent`` directly and pass the
``intent`` explicitly; this file exists only to avoid a coordinated
break across the tree.
"""

from __future__ import annotations

from alfred.agents.builders.schema_builder import (
	build_schema_agent,
)
from alfred.agents.builders.schema_builder import (
	enhance_generate_changeset_description as _enhance_schema,
)
from alfred.agents.builders.schema_builder import (
	render_registry_checklist as _render_checklist_with_intent,
)


def build_doctype_builder_agent(site_config: dict, custom_tools: dict | None):
	"""Compat alias for ``build_schema_agent("create_doctype", ...)``."""
	return build_schema_agent(
		intent="create_doctype",
		site_config=site_config,
		custom_tools=custom_tools,
	)


def enhance_generate_changeset_description(base: str, module_context: str = "") -> str:
	"""Compat alias for the schema-family enhancer bound to
	``intent="create_doctype"``.
	"""
	return _enhance_schema(
		base,
		intent="create_doctype",
		module_context=module_context,
	)


def render_registry_checklist(schema: dict) -> str:
	"""Compat alias for the schema-family checklist renderer bound to
	``intent="create_doctype"``.
	"""
	return _render_checklist_with_intent(schema, intent="create_doctype")
