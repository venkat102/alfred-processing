import os
from unittest.mock import patch

from alfred.agents.crew import (
	_enhance_task_description,
	_get_specialist_developer_agent,
	_per_intent_builders_enabled,
)
from alfred.config import get_settings as _get_settings


def test_flag_off_returns_false():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "0"}, clear=False):
		_get_settings.cache_clear()
		assert _per_intent_builders_enabled() is False


def test_flag_on_returns_true():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		assert _per_intent_builders_enabled() is True


def test_specialist_agent_none_when_flag_off():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "0"}, clear=False):
		_get_settings.cache_clear()
		result = _get_specialist_developer_agent(
			intent="create_doctype", site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_none_when_intent_unknown():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		result = _get_specialist_developer_agent(
			intent="unknown", site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_none_when_intent_none():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		result = _get_specialist_developer_agent(
			intent=None, site_config={}, custom_tools=None
		)
		assert result is None


def test_specialist_agent_returned_for_create_doctype():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		agent = _get_specialist_developer_agent(
			intent="create_doctype", site_config={}, custom_tools=None
		)
		assert agent is not None
		assert "DocType" in agent.role


def test_enhance_task_description_no_op_when_flag_off():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "0"}, clear=False):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text"
		)
		assert out == "base text"


def test_enhance_task_description_no_op_for_non_generate_changeset():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"gather_requirements", "create_doctype", "base text"
		)
		assert out == "base text"


def test_enhance_task_description_applies_for_generate_changeset_create_doctype():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text"
		)
		assert "base text" in out
		assert "field_defaults_meta" in out


def test_enhance_task_description_injects_module_context_when_provided():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text",
			module_context="accounts snippet",
		)
		assert "base text" in out
		assert "field_defaults_meta" in out
		assert "accounts snippet" in out


def test_enhance_task_description_ignores_module_context_for_other_tasks():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"gather_requirements", "create_doctype", "base text",
			module_context="accounts snippet",
		)
		assert out == "base text"


def test_enhance_task_description_empty_module_context_equals_v1_path():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text",
			module_context="",
		)
		assert "field_defaults_meta" in out
		assert "MODULE CONTEXT" not in out


def test_specialist_agent_returned_for_create_report():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		agent = _get_specialist_developer_agent(
			intent="create_report", site_config={}, custom_tools=None
		)
		assert agent is not None
		assert "Report" in agent.role


def test_enhance_task_description_applies_for_generate_changeset_create_report():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_report", "base text"
		)
		assert "base text" in out
		assert "ref_doctype" in out
		assert "report_type" in out


def test_enhance_task_description_injects_module_context_for_create_report():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_settings.cache_clear()
		out = _enhance_task_description(
			"generate_changeset", "create_report", "base text",
			module_context="selling snippet",
		)
		assert "base text" in out
		assert "ref_doctype" in out
		assert "selling snippet" in out
