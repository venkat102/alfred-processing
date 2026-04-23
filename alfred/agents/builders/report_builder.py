"""Compatibility shim - Report-specific builder APIs have moved into
``alfred.agents.builders.reports_builder`` as part of the Reports &
Insights specialist family (which also covers Dashboards, Dashboard
Charts, and Number Cards).

This module re-exports the old function names bound to
``intent="create_report"`` so existing callers (crew.py dispatcher
branches, test imports) keep working without edit. New code should
import ``build_reports_agent`` directly and pass ``intent``
explicitly.
"""

from __future__ import annotations

from alfred.agents.builders.reports_builder import (
	build_reports_agent,
)
from alfred.agents.builders.reports_builder import (
	enhance_generate_changeset_description as _enhance_reports,
)
from alfred.agents.builders.reports_builder import (
	render_registry_checklist as _render_checklist_with_intent,
)


def build_report_builder_agent(site_config: dict, custom_tools: dict | None):
	"""Compat alias for ``build_reports_agent("create_report", ...)``."""
	return build_reports_agent(
		intent="create_report",
		site_config=site_config,
		custom_tools=custom_tools,
	)


def enhance_generate_changeset_description(base: str, module_context: str = "") -> str:
	"""Compat alias for the reports-family enhancer bound to
	``intent="create_report"``.
	"""
	return _enhance_reports(
		base,
		intent="create_report",
		module_context=module_context,
	)


def render_registry_checklist(schema: dict) -> str:
	"""Compat alias for the reports-family checklist renderer bound to
	``intent="create_report"``.
	"""
	return _render_checklist_with_intent(schema, intent="create_report")
