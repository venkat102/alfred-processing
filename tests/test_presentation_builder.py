import pytest

from alfred.agents.builders.presentation_builder import (
	PRESENTATION_INTENTS,
	build_presentation_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry

# ── Intent set ────────────────────────────────────────────────

def test_presentation_intents_cover_the_family():
	assert PRESENTATION_INTENTS == frozenset({
		"create_print_format",
		"create_letter_head",
		"create_email_template",
		"create_web_form",
		"update_print_settings",
	})


# ── render_registry_checklist ────────────────────────────────

def test_render_checklist_print_format_fields():
	schema = IntentRegistry.load().get("create_print_format")
	text = render_registry_checklist(schema, intent="create_print_format")
	for key in ("name", "doc_type", "print_format_type", "html"):
		assert key in text


def test_render_checklist_letter_head_fields():
	schema = IntentRegistry.load().get("create_letter_head")
	text = render_registry_checklist(schema, intent="create_letter_head")
	for key in ("letter_head_name", "content", "footer", "is_default"):
		assert key in text


def test_render_checklist_email_template_fields():
	schema = IntentRegistry.load().get("create_email_template")
	text = render_registry_checklist(schema, intent="create_email_template")
	for key in ("name", "subject", "response", "use_html"):
		assert key in text


def test_render_checklist_web_form_fields():
	schema = IntentRegistry.load().get("create_web_form")
	text = render_registry_checklist(schema, intent="create_web_form")
	for key in ("title", "route", "doc_type", "login_required", "web_form_fields"):
		assert key in text


# ── build_presentation_agent ─────────────────────────────────

def test_build_presentation_agent_print_format():
	agent = build_presentation_agent(
		intent="create_print_format", site_config={}, custom_tools=None,
	)
	assert "Presentation" in agent.role
	assert "Print Format" in agent.role
	# Jinja vs Builder dichotomy is the headline quirk
	assert "Jinja" in agent.backstory


def test_build_presentation_agent_letter_head():
	agent = build_presentation_agent(
		intent="create_letter_head", site_config={}, custom_tools=None,
	)
	assert "Letter Head" in agent.role


def test_build_presentation_agent_email_template():
	agent = build_presentation_agent(
		intent="create_email_template", site_config={}, custom_tools=None,
	)
	assert "Email Template" in agent.role


def test_build_presentation_agent_web_form():
	agent = build_presentation_agent(
		intent="create_web_form", site_config={}, custom_tools=None,
	)
	assert "Web Form" in agent.role
	# web_form_fields whitelist is the load-bearing concept
	assert "web_form_fields" in agent.backstory


def test_build_presentation_agent_rejects_unknown_intent():
	with pytest.raises(ValueError):
		build_presentation_agent(
			intent="create_doctype", site_config={}, custom_tools=None,
		)


def test_build_presentation_agent_print_settings_intent():
	agent = build_presentation_agent(
		intent="update_print_settings", site_config={}, custom_tools=None,
	)
	assert "Print Settings" in agent.role
	# Singleton semantics must be in backstory
	assert "SINGLETON" in agent.backstory or "singleton" in agent.backstory.lower()


def test_render_checklist_print_settings_lists_every_field():
	schema = IntentRegistry.load().get("update_print_settings")
	text = render_registry_checklist(schema, intent="update_print_settings")
	for key in ("pdf_generator", "with_letterhead", "allow_print_for_draft", "allow_print_for_cancelled"):
		assert key in text


def test_print_format_has_margin_fields():
	# Regression: margins were missing from the prior registry,
	# preventing print-quality-sensitive prompts from landing.
	schema = IntentRegistry.load().get("create_print_format")
	field_keys = {f["key"] for f in schema["fields"]}
	assert "margin_top" in field_keys
	assert "margin_bottom" in field_keys
	assert "margin_left" in field_keys
	assert "margin_right" in field_keys


def test_web_form_has_seo_and_interlock_fields():
	schema = IntentRegistry.load().get("create_web_form")
	field_keys = {f["key"] for f in schema["fields"]}
	# SEO
	assert "meta_title" in field_keys
	assert "meta_description" in field_keys
	# Portal UX interlocks
	assert "introduction_text" in field_keys
	assert "show_list" in field_keys
	assert "show_sidebar" in field_keys


# ── enhance_generate_changeset_description ───────────────────

def test_enhance_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base, intent="create_print_format")
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_appends_intent_checklist_per_intent():
	out = enhance_generate_changeset_description("base", intent="create_web_form")
	assert "create_web_form" in out
	for key in ("title", "route", "doc_type"):
		assert key in out


def test_enhance_is_idempotent_per_intent():
	base = "base"
	once = enhance_generate_changeset_description(base, intent="create_letter_head")
	twice = enhance_generate_changeset_description(once, intent="create_letter_head")
	assert twice.count("SHAPE-DEFINING FIELDS for create_letter_head") == 1


def test_enhance_with_module_context_appends_both():
	out = enhance_generate_changeset_description(
		"BASE", intent="create_print_format", module_context="accounts convention",
	)
	assert "BASE" in out
	assert "accounts convention" in out
	assert "MODULE CONTEXT" in out


def test_enhance_rejects_unknown_intent():
	with pytest.raises(ValueError):
		enhance_generate_changeset_description(
			"BASE", intent="create_doctype",
		)


# ── ask-don't-assume contract ────────────────────────────────

def test_base_backstory_asks_dont_assume():
	agent = build_presentation_agent(
		intent="create_web_form", site_config={}, custom_tools=None,
	)
	assert "ASK, DO NOT ASSUME" in agent.backstory
	assert "needs_clarification" in agent.backstory


def test_checklist_exposes_needs_clarification_source():
	schema = IntentRegistry.load().get("create_web_form")
	text = render_registry_checklist(schema, intent="create_web_form")
	assert "needs_clarification" in text
