import pytest

from alfred.agents.builders.automation_builder import (
	AUTOMATION_INTENTS,
	build_automation_agent,
	enhance_generate_changeset_description,
	render_registry_checklist,
)
from alfred.registry.loader import IntentRegistry


# ── Intent set ────────────────────────────────────────────────

def test_automation_intents_cover_the_family():
	assert AUTOMATION_INTENTS == frozenset({
		"create_server_script",
		"create_client_script",
		"create_notification",
		"create_workflow",
	})


# ── render_registry_checklist ────────────────────────────────

def test_render_checklist_server_script_fields():
	schema = IntentRegistry.load().get("create_server_script")
	text = render_registry_checklist(schema, intent="create_server_script")
	for key in ("name", "script_type", "reference_doctype", "doctype_event", "script"):
		assert key in text


def test_render_checklist_client_script_fields():
	schema = IntentRegistry.load().get("create_client_script")
	text = render_registry_checklist(schema, intent="create_client_script")
	for key in ("name", "dt", "view", "script"):
		assert key in text


def test_render_checklist_notification_fields():
	schema = IntentRegistry.load().get("create_notification")
	text = render_registry_checklist(schema, intent="create_notification")
	for key in ("name", "document_type", "event", "channel", "recipients", "subject", "message"):
		assert key in text


def test_render_checklist_workflow_fields():
	schema = IntentRegistry.load().get("create_workflow")
	text = render_registry_checklist(schema, intent="create_workflow")
	for key in ("workflow_name", "document_type", "is_active", "states", "transitions"):
		assert key in text


# ── build_automation_agent ───────────────────────────────────

def test_build_automation_agent_server_script():
	agent = build_automation_agent(
		intent="create_server_script", site_config={}, custom_tools=None,
	)
	assert "Automation" in agent.role
	assert "Server Script" in agent.role
	# RestrictedPython constraint is the headline quirk; must be in backstory
	assert "RestrictedPython" in agent.backstory
	assert "import" in agent.backstory.lower()


def test_build_automation_agent_client_script():
	agent = build_automation_agent(
		intent="create_client_script", site_config={}, custom_tools=None,
	)
	assert "Client Script" in agent.role


def test_build_automation_agent_notification():
	agent = build_automation_agent(
		intent="create_notification", site_config={}, custom_tools=None,
	)
	assert "Notification" in agent.role
	# event-choice lore is the headline quirk
	assert "event" in agent.backstory.lower()


def test_build_automation_agent_workflow():
	agent = build_automation_agent(
		intent="create_workflow", site_config={}, custom_tools=None,
	)
	assert "Workflow" in agent.role
	# workflow_state Custom Field auto-creation is a frequent drift point
	assert "workflow_state" in agent.backstory.lower() or "three linked docs" in agent.backstory.lower()


def test_build_automation_agent_rejects_unknown_intent():
	with pytest.raises(ValueError):
		build_automation_agent(
			intent="create_doctype", site_config={}, custom_tools=None,
		)


# ── enhance_generate_changeset_description ───────────────────

def test_enhance_preserves_base():
	base = "BASE DESCRIPTION with {design} placeholder"
	out = enhance_generate_changeset_description(base, intent="create_notification")
	assert "BASE DESCRIPTION with {design} placeholder" in out


def test_enhance_appends_intent_checklist_per_intent():
	out = enhance_generate_changeset_description("base", intent="create_server_script")
	assert "create_server_script" in out
	for key in ("script_type", "script"):
		assert key in out


def test_enhance_is_idempotent_per_intent():
	base = "base"
	once = enhance_generate_changeset_description(base, intent="create_workflow")
	twice = enhance_generate_changeset_description(once, intent="create_workflow")
	assert twice.count("SHAPE-DEFINING FIELDS for create_workflow") == 1


def test_enhance_with_module_context_appends_both():
	out = enhance_generate_changeset_description(
		"BASE", intent="create_notification", module_context="hr convention snippet",
	)
	assert "BASE" in out
	assert "hr convention snippet" in out
	assert "MODULE CONTEXT" in out


def test_enhance_rejects_unknown_intent():
	with pytest.raises(ValueError):
		enhance_generate_changeset_description(
			"BASE", intent="create_doctype",
		)


# ── ask-don't-assume contract ────────────────────────────────

def test_base_backstory_asks_dont_assume():
	agent = build_automation_agent(
		intent="create_notification", site_config={}, custom_tools=None,
	)
	assert "ASK, DO NOT ASSUME" in agent.backstory
	assert "needs_clarification" in agent.backstory


def test_checklist_exposes_needs_clarification_source():
	schema = IntentRegistry.load().get("create_notification")
	text = render_registry_checklist(schema, intent="create_notification")
	assert "needs_clarification" in text
