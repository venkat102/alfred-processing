from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext


@pytest.fixture(autouse=True)
def _reset_settings_cache():
	# Most tests in this module flip ALFRED_PER_INTENT_BUILDERS /
	# ALFRED_MODULE_SPECIALISTS via monkeypatch.setenv or .delenv and
	# then expect ``_phase_classify_module`` / ``_phase_provide_module_context``
	# to read the new value. Those phases consult ``get_settings()``,
	# which is ``@lru_cache``d, so without this reset every test after
	# the first sees the Settings snapshot taken by the earliest test.
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


def test_phases_includes_classify_module_and_provide_module_context_in_order():
	phases = AgentPipeline.PHASES
	assert "classify_module" in phases
	assert "provide_module_context" in phases
	assert phases.index("classify_intent") < phases.index("classify_module")
	assert phases.index("classify_module") < phases.index("provide_module_context")
	assert phases.index("provide_module_context") < phases.index("build_crew")


@pytest.mark.asyncio
async def test_classify_module_noop_for_non_dev_mode(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice", mode="plan")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_noop_when_v2_flag_off(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "0")
	ctx = _build_ctx("Customize Sales Invoice", mode="dev")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_noop_when_v1_flag_off(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "0")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice", mode="dev")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_populates_ctx_on_heuristic_match(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize journal entry with a new field")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module == "accounts"
	assert ctx.module_source == "heuristic"


@pytest.mark.asyncio
async def test_provide_module_context_noop_when_module_is_none(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("prompt")
	ctx.module = None
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_provide_module_context()
	assert ctx.module_context == ""


@pytest.mark.asyncio
async def test_provide_module_context_calls_specialist_when_module_set(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	# V2 single-module path — disable V3 so the specialist's snippet
	# flows through verbatim (no PRIMARY MODULE / FAMILY wrapper).
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "0")
	ctx = _build_ctx("prompt")
	ctx.module = "accounts"
	ctx.intent = "create_doctype"
	pipeline = AgentPipeline(ctx)
	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(return_value="accounts snippet"),
	) as spy:
		await pipeline._phase_provide_module_context()
		spy.assert_awaited_once()
		assert ctx.module_context == "accounts snippet"


def test_phase_ordering_classify_module_before_enhance_and_provide_after():
	# Regression guard per code review: classify_module runs early (right
	# after classify_intent) so enhance/inject_kb can see ctx.module if
	# they ever grow that dependency; provide_module_context runs AFTER
	# resolve_mode so the crew build can use the resulting context.
	phases = AgentPipeline.PHASES
	assert phases.index("classify_intent") < phases.index("classify_module")
	assert phases.index("classify_module") < phases.index("enhance")
	assert phases.index("resolve_mode") < phases.index("provide_module_context")
	assert phases.index("provide_module_context") < phases.index("build_crew")


@pytest.mark.asyncio
async def test_classify_module_noop_when_v1_off_but_v2_on_with_prior_intent(monkeypatch):
	# Cross-flag race: V2 flag on, V1 flag off, ctx.intent already set from
	# a prior turn's V1 run. Module classification must still be a no-op
	# because V2 depends on V1's prompt-enhancement path being active.
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "0")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice")
	ctx.intent = "create_doctype"  # stale from prior turn
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None
	assert ctx.module_source is None
