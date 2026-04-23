import pytest

from alfred.agents.builders.reports_builder import (
	REPORTS_INTENTS,
	build_reports_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry


# ── Intent set ────────────────────────────────────────────────

def test_reports_intents_cover_the_family():
	assert REPORTS_INTENTS == frozenset({
		"create_report",
		"create_dashboard",
		"create_dashboard_chart",
		"create_number_card",
	})


# ── render_registry_checklist ────────────────────────────────

def test_render_checklist_report_lists_every_field():
	schema = IntentRegistry.load().get("create_report")
	text = render_registry_checklist(schema, intent="create_report")
	for key in ("ref_doctype", "report_type", "is_standard", "module"):
		assert key in text
	assert "field_defaults_meta" in text


def test_render_checklist_dashboard_lists_every_field():
	schema = IntentRegistry.load().get("create_dashboard")
	text = render_registry_checklist(schema, intent="create_dashboard")
	for key in ("dashboard_name", "module", "is_standard", "chart_options"):
		assert key in text


def test_render_checklist_chart_lists_every_field():
	schema = IntentRegistry.load().get("create_dashboard_chart")
	text = render_registry_checklist(schema, intent="create_dashboard_chart")
	for key in ("chart_name", "source", "chart_type", "timespan", "based_on", "value_based_on"):
		assert key in text


def test_render_checklist_number_card_lists_every_field():
	schema = IntentRegistry.load().get("create_number_card")
	text = render_registry_checklist(schema, intent="create_number_card")
	for key in ("label", "document_type", "function", "aggregate_function_based_on", "filters_json"):
		assert key in text


# ── build_reports_agent ──────────────────────────────────────

def test_build_reports_agent_report_intent():
	agent = build_reports_agent(
		intent="create_report", site_config={}, custom_tools=None,
	)
	assert "Reports" in agent.role
	assert "Report" in agent.role


def test_build_reports_agent_dashboard_intent():
	agent = build_reports_agent(
		intent="create_dashboard", site_config={}, custom_tools=None,
	)
	assert "Dashboard" in agent.role
	# Family backstory should teach that Dashboards are layout
	assert "layout" in agent.backstory.lower() or "compose" in agent.backstory.lower()


def test_build_reports_agent_chart_intent():
	agent = build_reports_agent(
		intent="create_dashboard_chart", site_config={}, custom_tools=None,
	)
	assert "Dashboard Chart" in agent.role
	# Backstory should teach the Report vs DocType source dichotomy
	assert "source" in agent.backstory.lower()


def test_build_reports_agent_number_card_intent():
	agent = build_reports_agent(
		intent="create_number_card", site_config={}, custom_tools=None,
	)
	assert "Number Card" in agent.role


def test_build_reports_agent_rejects_unknown_intent():
	with pytest.raises(ValueError):
		build_reports_agent(
			intent="create_workflow", site_config={}, custom_tools=None,
		)


# ── enhance_generate_changeset_description ───────────────────

def test_enhance_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base, intent="create_report")
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_appends_intent_checklist_per_intent():
	out = enhance_generate_changeset_description("base", intent="create_number_card")
	assert "create_number_card" in out
	for key in ("label", "document_type", "function"):
		assert key in out


def test_enhance_is_idempotent_per_intent():
	base = "base"
	once = enhance_generate_changeset_description(base, intent="create_dashboard")
	twice = enhance_generate_changeset_description(once, intent="create_dashboard")
	assert twice.count("SHAPE-DEFINING FIELDS for create_dashboard") == 1


def test_enhance_with_module_context_appends_both():
	out = enhance_generate_changeset_description(
		"BASE", intent="create_report", module_context="accounts convention snippet",
	)
	assert "BASE" in out
	assert "accounts convention snippet" in out
	assert "MODULE CONTEXT" in out


def test_enhance_rejects_unknown_intent():
	with pytest.raises(ValueError):
		enhance_generate_changeset_description(
			"BASE", intent="create_workflow",
		)


# ── ask-don't-assume contract ────────────────────────────────

def test_base_backstory_asks_dont_assume():
	agent = build_reports_agent(
		intent="create_report", site_config={}, custom_tools=None,
	)
	assert "ASK, DO NOT ASSUME" in agent.backstory
	assert "needs_clarification" in agent.backstory


def test_checklist_exposes_needs_clarification_source():
	schema = IntentRegistry.load().get("create_report")
	text = render_registry_checklist(schema, intent="create_report")
	assert "needs_clarification" in text


# ── compat shim stays wired ──────────────────────────────────

def test_old_report_builder_api_still_works():
	from alfred.agents.builders.report_builder import (
		build_report_builder_agent,
		enhance_generate_changeset_description as legacy_enhance,
		render_registry_checklist as legacy_render,
	)
	agent = build_report_builder_agent(site_config={}, custom_tools=None)
	assert "Report" in agent.role
	out = legacy_enhance("BASE")
	assert "SHAPE-DEFINING FIELDS for create_report" in out
	schema = IntentRegistry.load().get("create_report")
	rendered = legacy_render(schema)
	assert "ref_doctype" in rendered
