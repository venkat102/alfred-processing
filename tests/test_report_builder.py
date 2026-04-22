from alfred.agents.builders.report_builder import (
	build_report_builder_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry


def test_render_registry_checklist_lists_every_field():
	schema = IntentRegistry.load().get("create_report")
	text = render_registry_checklist(schema)
	for key in ("ref_doctype", "report_type", "is_standard", "module"):
		assert key in text


def test_render_registry_checklist_mentions_field_defaults_meta():
	schema = IntentRegistry.load().get("create_report")
	text = render_registry_checklist(schema)
	assert "field_defaults_meta" in text


def test_build_report_builder_agent_returns_agent_with_report_backstory():
	agent = build_report_builder_agent(site_config={}, custom_tools=None)
	assert "Report" in agent.backstory
	assert "specialis" in agent.backstory.lower()


def test_build_report_builder_agent_role_identifies_specialist():
	agent = build_report_builder_agent(site_config={}, custom_tools=None)
	assert "Report" in agent.role


def test_enhance_generate_changeset_description_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base)
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_generate_changeset_description_appends_checklist():
	base = "base"
	out = enhance_generate_changeset_description(base)
	assert "field_defaults_meta" in out
	for key in ("ref_doctype", "report_type", "is_standard", "module"):
		assert key in out


def test_enhance_generate_changeset_description_is_idempotent():
	base = "base"
	once = enhance_generate_changeset_description(base)
	twice = enhance_generate_changeset_description(once)
	assert twice.count("SHAPE-DEFINING FIELDS for create_report") == 1


def test_enhance_with_module_context_appends_both_sections():
	base = "BASE"
	out = enhance_generate_changeset_description(base, module_context="selling snippet")
	assert "BASE" in out
	assert "ref_doctype" in out
	assert "selling snippet" in out
	assert "MODULE CONTEXT" in out


def test_enhance_with_empty_module_context_no_module_section():
	base = "BASE"
	out = enhance_generate_changeset_description(base, module_context="")
	assert "BASE" in out
	assert "ref_doctype" in out
	assert "MODULE CONTEXT" not in out


def test_enhance_with_module_context_is_idempotent():
	base = "BASE"
	once = enhance_generate_changeset_description(base, module_context="snip")
	twice = enhance_generate_changeset_description(once, module_context="snip")
	assert once == twice
