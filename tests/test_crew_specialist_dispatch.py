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


# ── Fallback observability logs ──────────────────────────────────────
# #P4: when the specialist stack can't dispatch, the fallback to the
# generic Developer must be loud enough that operators can diagnose it
# from logs alone. Silent fallback was the source of "why did my
# create_doctype prompt use the generic agent?" mysteries.


def test_unknown_intent_logs_info_level(caplog):
	import logging
	caplog.set_level(logging.INFO, logger="alfred.crew")
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_specialist_developer_agent(
			intent="unknown", site_config={}, custom_tools=None,
		)
	# Classifier-punt case: INFO level, names the intent.
	matching = [r for r in caplog.records if "falling back to generic Developer" in r.message]
	assert matching, f"Expected a fallback log line, got: {[r.message for r in caplog.records]}"
	assert any(r.levelname == "INFO" for r in matching)
	assert any("unknown" in r.message for r in matching)


def test_none_intent_logs_info_level(caplog):
	import logging
	caplog.set_level(logging.INFO, logger="alfred.crew")
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_specialist_developer_agent(
			intent=None, site_config={}, custom_tools=None,
		)
	matching = [r for r in caplog.records if "falling back to generic Developer" in r.message]
	assert matching


def test_unrecognised_intent_logs_warning(caplog):
	# Intent is non-empty and not "unknown", but no family claims it.
	# This is the "drift" case - classifier said something the builder
	# catalog doesn't know about. Raises to WARNING so it shows up in
	# dashboards.
	import logging
	caplog.set_level(logging.WARNING, logger="alfred.crew")
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		_get_specialist_developer_agent(
			intent="make_me_a_sandwich", site_config={}, custom_tools=None,
		)
	matching = [
		r for r in caplog.records
		if "No builder registered for intent" in r.message
		and r.levelname == "WARNING"
	]
	assert matching, f"Expected WARNING log line, got: {[(r.levelname, r.message) for r in caplog.records]}"
	assert any("make_me_a_sandwich" in r.message for r in matching)


def test_flag_off_does_not_log(caplog):
	# Flag-off is the default case - must stay silent so logs don't
	# drown in "no specialist dispatched" noise on every prompt.
	import logging
	caplog.set_level(logging.DEBUG, logger="alfred.crew")
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		_get_specialist_developer_agent(
			intent="create_doctype", site_config={}, custom_tools=None,
		)
	fallback_lines = [r for r in caplog.records if "falling back" in r.message]
	assert not fallback_lines
