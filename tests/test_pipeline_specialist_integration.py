"""Integration-level tests for Dev-mode pipeline wiring around per-intent Builder.

Exercises:
  1. The PHASES list includes classify_intent between orchestrate and enhance.
  2. classify_intent is a no-op when mode != dev.
  3. classify_intent is a no-op when ALFRED_PER_INTENT_BUILDERS is unset.
  4. classify_intent populates ctx.intent* fields from the heuristic matcher.
  5. The flag-gated backfill path produces the expected annotated changeset.

The tests drive ``_phase_classify_intent`` directly rather than standing up
the whole pipeline; that's enough to verify wiring without a Redis / MCP /
crew harness.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext
from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


@pytest.fixture(autouse=True)
def _reset_settings_cache():
	from alfred.config import get_settings
	get_settings.cache_clear()
	yield
	get_settings.cache_clear()


def _build_ctx(prompt: str, mode: str = "dev") -> PipelineContext:
	conn = MagicMock()
	conn.site_config = {}
	ctx = PipelineContext(conn=conn, conversation_id="test-conv", prompt=prompt)
	ctx.mode = mode
	return ctx


def test_phases_list_includes_classify_intent_between_orchestrate_and_enhance():
	phases = AgentPipeline.PHASES
	assert "classify_intent" in phases
	assert phases.index("orchestrate") < phases.index("classify_intent")
	assert phases.index("classify_intent") < phases.index("enhance")
	assert phases.index("classify_intent") < phases.index("build_crew")


@pytest.mark.asyncio
async def test_classify_intent_noop_for_non_dev_mode(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	ctx = _build_ctx("Create a DocType called Book", mode="plan")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_intent()
	assert ctx.intent is None


@pytest.mark.asyncio
async def test_classify_intent_noop_when_flag_off(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "0")
	ctx = _build_ctx("Create a DocType called Book", mode="dev")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_intent()
	assert ctx.intent is None


@pytest.mark.asyncio
async def test_classify_intent_populates_ctx_on_heuristic_match(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	ctx = _build_ctx(
		"Create a DocType called Book with title, author, and ISBN fields",
		mode="dev",
	)
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_intent()
	assert ctx.intent == "create_doctype"
	assert ctx.intent_source == "heuristic"
	assert ctx.intent_confidence == "high"


def test_pipeline_backfill_on_extracted_changes_shape():
	"""Contract test for the change shape that _extract_changes produces.

	If the pipeline path runs through backfill_defaults_raw, a partial
	DocType change ends up with registry fields populated and a
	field_defaults_meta dict added alongside ``data``.
	"""
	raw = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {
				"name": "Book",
				"module": "Custom",
				"fields": [
					{"fieldname": "title", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "author", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "isbn", "fieldtype": "Data", "unique": 1},
				],
			},
		},
	]
	out = backfill_defaults_raw(raw)
	item = out[0]
	assert item["data"]["autoname"] == "autoincrement"
	assert item["data"]["is_submittable"] == 0
	assert isinstance(item["data"]["permissions"], list)
	meta = item["field_defaults_meta"]
	assert meta["autoname"]["source"] == "default"
	assert meta["autoname"]["rationale"]
	assert meta["module"]["source"] == "user"
