from alfred.agents.builders.doctype_builder import (
	build_doctype_builder_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry


def test_render_registry_checklist_lists_every_field():
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema)
	for key in ("module", "is_submittable", "autoname", "istable", "issingle", "permissions"):
		assert key in text


def test_render_registry_checklist_mentions_field_defaults_meta():
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema)
	assert "field_defaults_meta" in text


def test_build_doctype_builder_agent_returns_agent_with_doctype_backstory():
	agent = build_doctype_builder_agent(site_config={}, custom_tools=None)
	assert "DocType" in agent.backstory


def test_build_doctype_builder_agent_role_identifies_specialist():
	agent = build_doctype_builder_agent(site_config={}, custom_tools=None)
	# Role stays close to the generic "Frappe Developer" but flags the specialty
	assert "DocType" in agent.role or "doctype" in agent.role.lower()


def test_enhance_generate_changeset_description_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base)
	# Base content is preserved (so format_vars still interpolates later)
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_generate_changeset_description_appends_checklist():
	base = "base"
	out = enhance_generate_changeset_description(base)
	assert "field_defaults_meta" in out
	for key in ("module", "is_submittable", "autoname", "istable", "issingle", "permissions"):
		assert key in out


def test_enhance_generate_changeset_description_is_idempotent():
	base = "base"
	once = enhance_generate_changeset_description(base)
	twice = enhance_generate_changeset_description(once)
	# Double-enhancing should not double-append the checklist (defensive for flag flicker)
	assert twice.count("SHAPE-DEFINING FIELDS for create_doctype") == 1
