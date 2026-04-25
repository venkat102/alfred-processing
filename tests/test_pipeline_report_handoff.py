"""Tests for the Insights -> Report handoff pipeline path."""

import json
from unittest.mock import MagicMock

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext, _parse_report_candidate_marker


@pytest.fixture(autouse=True)
def _reset_settings_cache():
	from alfred.config import get_settings
	get_settings.cache_clear()
	yield
	get_settings.cache_clear()


def _ctx(prompt: str) -> PipelineContext:
	conn = MagicMock()
	conn.site_config = {}
	c = PipelineContext(conn=conn, conversation_id="t", prompt=prompt)
	c.mode = "dev"
	return c


def test_parse_marker_absent_returns_none():
	assert _parse_report_candidate_marker("plain prompt") is None
	assert _parse_report_candidate_marker("") is None


def test_parse_marker_valid_json_returns_dict():
	prompt = 'Save as Report\n__report_candidate__: {"target_doctype": "Customer", "limit": 10}'
	parsed = _parse_report_candidate_marker(prompt)
	assert parsed == {"target_doctype": "Customer", "limit": 10}


def test_parse_marker_malformed_json_returns_none():
	prompt = "__report_candidate__: {not valid json}"
	assert _parse_report_candidate_marker(prompt) is None


def test_parse_marker_case_insensitive():
	prompt = '__Report_Candidate__: {"target_doctype": "Customer"}'
	parsed = _parse_report_candidate_marker(prompt)
	assert parsed == {"target_doctype": "Customer"}


@pytest.mark.asyncio
async def test_classify_intent_short_circuits_on_handoff_marker(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_REPORT_HANDOFF", "1")
	candidate = {"target_doctype": "Customer", "report_type": "Report Builder", "limit": 10}
	prompt = (
		"Save as Report\n"
		"Source DocType: Customer\n"
		"Report type: Report Builder\n"
		f"__report_candidate__: {json.dumps(candidate)}"
	)
	c = _ctx(prompt)
	p = AgentPipeline(c)
	await p._phase_classify_intent()
	assert c.intent == "create_report"
	assert c.intent_source == "handoff"
	assert c.intent_confidence == "high"
	assert c.report_candidate == candidate


@pytest.mark.asyncio
async def test_handoff_flag_off_does_not_short_circuit(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_REPORT_HANDOFF", "0")
	prompt = '__report_candidate__: {"target_doctype": "Customer"}'
	c = _ctx(prompt)
	p = AgentPipeline(c)
	await p._phase_classify_intent()
	# No handoff - normal classifier path. "report_candidate" in the prompt
	# might still trigger heuristic hit on "save as report" phrase, but the
	# source should NOT be "handoff".
	assert c.intent_source != "handoff"
	assert c.report_candidate is None


@pytest.mark.asyncio
async def test_prompt_without_marker_runs_normal_classifier(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_REPORT_HANDOFF", "1")
	c = _ctx("Create a DocType called Book with title, author fields")
	p = AgentPipeline(c)
	await p._phase_classify_intent()
	# Normal heuristic should still route to create_doctype
	assert c.intent == "create_doctype"
	assert c.intent_source == "heuristic"
	assert c.report_candidate is None


@pytest.mark.asyncio
async def test_handoff_short_circuit_noop_for_non_dev_mode(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_REPORT_HANDOFF", "1")
	candidate = {"target_doctype": "Customer"}
	c = _ctx(f"__report_candidate__: {json.dumps(candidate)}")
	c.mode = "plan"
	p = AgentPipeline(c)
	await p._phase_classify_intent()
	# classify_intent is a no-op for non-dev modes regardless of marker
	assert c.intent is None
	assert c.report_candidate is None
