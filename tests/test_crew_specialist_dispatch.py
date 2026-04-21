import os
from unittest.mock import patch

from alfred.agents.crew import (
	_enhance_task_description,
	_get_specialist_developer_agent,
	_per_intent_builders_enabled,
)


def test_flag_off_returns_false():
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		assert _per_intent_builders_enabled() is False


def test_flag_on_returns_true():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		assert _per_intent_builders_enabled() is True


def test_specialist_agent_none_when_flag_off():
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		result = _get_specialist_developer_agent(
			intent="create_doctype", site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_none_when_intent_unknown():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		result = _get_specialist_developer_agent(
			intent="unknown", site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_none_when_intent_none():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		result = _get_specialist_developer_agent(
			intent=None, site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_returned_for_create_doctype():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		agent = _get_specialist_developer_agent(
			intent="create_doctype", site_config={}, custom_tools=None
		)
		assert agent is not None
		assert "DocType" in agent.role


def test_enhance_task_description_no_op_when_flag_off():
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text"
		)
		assert out == "base text"


def test_enhance_task_description_no_op_for_non_generate_changeset():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		out = _enhance_task_description(
			"gather_requirements", "create_doctype", "base text"
		)
		assert out == "base text"


def test_enhance_task_description_applies_for_generate_changeset_create_doctype():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text"
		)
		assert "base text" in out
		assert "field_defaults_meta" in out
