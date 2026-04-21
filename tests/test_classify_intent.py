from unittest.mock import AsyncMock, patch

import pytest

from alfred.orchestrator import IntentDecision, classify_intent


@pytest.mark.asyncio
async def test_heuristic_matches_create_doctype():
	decision = await classify_intent(
		"Create a DocType called Book with title, author, and ISBN fields",
		site_config={},
	)
	assert decision.intent == "create_doctype"
	assert decision.source == "heuristic"
	assert decision.confidence == "high"


@pytest.mark.asyncio
async def test_heuristic_matches_new_doctype():
	decision = await classify_intent("new doctype Employee", site_config={})
	assert decision.intent == "create_doctype"
	assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_miss_calls_classifier():
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(return_value="create_doctype"),
	) as llm:
		decision = await classify_intent(
			"I need some kind of structured thing for books maybe",
			site_config={"llm_tier": "triage"},
		)
		llm.assert_awaited_once()
		assert decision.intent == "create_doctype"
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_classifier_returns_unknown_on_no_match():
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(return_value="unknown"),
	):
		decision = await classify_intent(
			"absolutely nothing useful here", site_config={}
		)
		assert decision.intent == "unknown"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_unknown():
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		decision = await classify_intent("totally unclear prompt", site_config={})
		assert decision.intent == "unknown"
		assert decision.source == "fallback"


def test_intent_decision_to_dict():
	d = IntentDecision(
		intent="create_doctype", confidence="high", source="heuristic", reason="x"
	)
	assert d.to_dict() == {
		"intent": "create_doctype",
		"confidence": "high",
		"source": "heuristic",
		"reason": "x",
	}
