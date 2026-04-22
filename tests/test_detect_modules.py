from unittest.mock import AsyncMock, patch

import pytest

from alfred.orchestrator import ModulesDecision, detect_modules


@pytest.mark.asyncio
async def test_heuristic_primary_only():
	d = await detect_modules(
		prompt="Customize Sales Invoice",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert d.module == "accounts"
	assert d.secondary_modules == []
	assert d.source == "heuristic"
	assert d.confidence == "high"


@pytest.mark.asyncio
async def test_heuristic_primary_plus_secondary():
	d = await detect_modules(
		prompt="Sales Invoice that auto-creates a project task",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert d.module == "accounts"
	assert "projects" in d.secondary_modules
	assert d.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_miss_llm_fallback_primary_only():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="accounts"),
	):
		d = await detect_modules(
			prompt="some vague request that has no known keyword hit",
			target_doctype=None,
			site_config={"llm_tier": "triage"},
		)
		assert d.module == "accounts"
		assert d.secondary_modules == []
		assert d.source == "classifier"


@pytest.mark.asyncio
async def test_unknown_returns_empty_decision():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="unknown"),
	):
		d = await detect_modules(
			prompt="hello goodbye",
			target_doctype=None,
			site_config={},
		)
		assert d.module is None
		assert d.secondary_modules == []
		assert d.source == "classifier"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_none():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		d = await detect_modules(
			prompt="hello goodbye",
			target_doctype=None,
			site_config={},
		)
		assert d.module is None
		assert d.source == "fallback"


def test_modules_decision_to_dict():
	d = ModulesDecision(
		module="accounts", secondary_modules=["projects"],
		reason="r", confidence="high", source="heuristic",
	)
	assert d.to_dict() == {
		"module": "accounts",
		"secondary_modules": ["projects"],
		"reason": "r",
		"confidence": "high",
		"source": "heuristic",
	}
