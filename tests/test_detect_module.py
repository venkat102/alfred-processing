from unittest.mock import AsyncMock, patch

import pytest

from alfred.orchestrator import ModuleDecision, detect_module


@pytest.mark.asyncio
async def test_heuristic_matches_via_target_doctype():
	decision = await detect_module(
		prompt="Customize Sales Invoice adding a field",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert decision.module == "accounts"
	assert decision.source == "heuristic"
	assert decision.confidence == "high"


@pytest.mark.asyncio
async def test_heuristic_matches_via_keyword():
	decision = await detect_module(
		prompt="create a journal entry customization",
		target_doctype=None,
		site_config={},
	)
	assert decision.module == "accounts"
	assert decision.source == "heuristic"
	assert decision.confidence == "medium"


@pytest.mark.asyncio
async def test_heuristic_miss_calls_llm():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="accounts"),
	) as llm:
		decision = await detect_module(
			prompt="help me reconcile period balances at month end",
			target_doctype=None,
			site_config={"llm_tier": "triage"},
		)
		llm.assert_awaited_once()
		assert decision.module == "accounts"
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_llm_returns_unknown():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="unknown"),
	):
		decision = await detect_module(
			prompt="generic prompt", target_doctype=None, site_config={},
		)
		assert decision.module is None
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_unknown():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		decision = await detect_module(
			prompt="generic prompt", target_doctype=None, site_config={},
		)
		assert decision.module is None
		assert decision.source == "fallback"


def test_decision_to_dict():
	d = ModuleDecision(module="accounts", reason="r", confidence="high", source="heuristic")
	assert d.to_dict() == {
		"module": "accounts",
		"reason": "r",
		"confidence": "high",
		"source": "heuristic",
	}
