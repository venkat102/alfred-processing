import pytest

from alfred.agents.builders.schema_builder import (
	SCHEMA_INTENTS,
	build_schema_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry


# ── Intent set ────────────────────────────────────────────────

def test_schema_intents_cover_the_family():
	assert SCHEMA_INTENTS == frozenset({
		"create_doctype",
		"create_custom_field",
		"create_role_with_permissions",
	})


# ── render_registry_checklist ────────────────────────────────

def test_render_checklist_doctype_lists_every_field():
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema, intent="create_doctype")
	for key in ("module", "is_submittable", "autoname", "istable", "issingle", "permissions"):
		assert key in text
	assert "field_defaults_meta" in text


def test_render_checklist_custom_field_lists_every_field():
	schema = IntentRegistry.load().get("create_custom_field")
	text = render_registry_checklist(schema, intent="create_custom_field")
	for key in ("dt", "fieldname", "label", "fieldtype", "insert_after"):
		assert key in text


def test_render_checklist_role_lists_every_field():
	schema = IntentRegistry.load().get("create_role_with_permissions")
	text = render_registry_checklist(schema, intent="create_role_with_permissions")
	for key in ("role_name", "target_doctypes", "desk_access", "permlevel", "action_flags"):
		assert key in text


def test_render_checklist_marker_is_intent_specific():
	# Distinct markers per intent so multi-intent prompts can
	# stack checklists without clobbering.
	schema = IntentRegistry.load().get("create_doctype")
	doctype_text = render_registry_checklist(schema, intent="create_doctype")
	field_schema = IntentRegistry.load().get("create_custom_field")
	field_text = render_registry_checklist(field_schema, intent="create_custom_field")
	assert "create_doctype" in doctype_text
	assert "create_custom_field" in field_text
	assert "create_custom_field" not in doctype_text


# ── build_schema_agent ───────────────────────────────────────

def test_build_schema_agent_doctype_intent():
	agent = build_schema_agent(
		intent="create_doctype", site_config={}, custom_tools=None,
	)
	assert "Schema" in agent.role
	assert "DocType" in agent.role
	assert "Schema" in agent.backstory or "schema" in agent.backstory.lower()


def test_build_schema_agent_custom_field_intent():
	agent = build_schema_agent(
		intent="create_custom_field", site_config={}, custom_tools=None,
	)
	assert "Custom Field" in agent.role
	# Family backstory should teach the dt-vs-doctype distinction
	assert "dt" in agent.backstory.lower() or "target" in agent.backstory.lower()


def test_build_schema_agent_role_permissions_intent():
	agent = build_schema_agent(
		intent="create_role_with_permissions", site_config={}, custom_tools=None,
	)
	assert "Role" in agent.role
	# Family backstory should mention Custom DocPerm
	assert "Custom DocPerm" in agent.backstory


def test_build_schema_agent_rejects_unknown_intent():
	with pytest.raises(ValueError):
		build_schema_agent(
			intent="create_workflow", site_config={}, custom_tools=None,
		)


# ── enhance_generate_changeset_description ───────────────────

def test_enhance_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base, intent="create_doctype")
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_appends_intent_checklist():
	out = enhance_generate_changeset_description("base", intent="create_custom_field")
	assert "create_custom_field" in out
	for key in ("dt", "fieldname", "fieldtype"):
		assert key in out


def test_enhance_is_idempotent_per_intent():
	base = "base"
	once = enhance_generate_changeset_description(base, intent="create_doctype")
	twice = enhance_generate_changeset_description(once, intent="create_doctype")
	assert twice.count("SHAPE-DEFINING FIELDS for create_doctype") == 1


def test_enhance_with_module_context_appends_both():
	out = enhance_generate_changeset_description(
		"BASE", intent="create_doctype", module_context="accounts convention snippet",
	)
	assert "BASE" in out
	assert "field_defaults_meta" in out
	assert "accounts convention snippet" in out


def test_enhance_empty_module_context_skips_module_section():
	out = enhance_generate_changeset_description(
		"BASE", intent="create_doctype", module_context="",
	)
	assert "BASE" in out
	assert "MODULE CONTEXT" not in out


def test_enhance_with_module_context_is_idempotent():
	once = enhance_generate_changeset_description(
		"BASE", intent="create_doctype", module_context="snip",
	)
	twice = enhance_generate_changeset_description(
		once, intent="create_doctype", module_context="snip",
	)
	assert once == twice


def test_enhance_rejects_unknown_intent():
	with pytest.raises(ValueError):
		enhance_generate_changeset_description(
			"BASE", intent="create_workflow",
		)


# ── ask-don't-assume contract ────────────────────────────────

def test_base_backstory_asks_dont_assume():
	agent = build_schema_agent(
		intent="create_doctype", site_config={}, custom_tools=None,
	)
	assert "ASK, DO NOT ASSUME" in agent.backstory
	assert "needs_clarification" in agent.backstory


def test_checklist_exposes_needs_clarification_source():
	# The rendered prompt must teach the model the third field_defaults_meta
	# source ("needs_clarification"), not just "user" / "default".
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema, intent="create_doctype")
	assert "needs_clarification" in text


# ── compat shim stays wired ──────────────────────────────────

def test_old_doctype_builder_api_still_works():
	# doctype_builder.py remained as a compat shim re-exporting from
	# schema_builder so existing callers (crew dispatcher, legacy
	# tests) don't break during the rename.
	from alfred.agents.builders.doctype_builder import (
		build_doctype_builder_agent,
		enhance_generate_changeset_description as legacy_enhance,
		render_registry_checklist as legacy_render,
	)
	agent = build_doctype_builder_agent(site_config={}, custom_tools=None)
	assert "DocType" in agent.role
	out = legacy_enhance("BASE")
	assert "SHAPE-DEFINING FIELDS for create_doctype" in out
	schema = IntentRegistry.load().get("create_doctype")
	rendered = legacy_render(schema)
	assert "module" in rendered
