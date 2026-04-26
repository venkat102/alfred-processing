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


@pytest.mark.asyncio
async def test_heuristic_matches_save_as_report():
	decision = await classify_intent(
		"Save this as a report for next time",
		site_config={},
	)
	assert decision.intent == "create_report"
	assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_matches_create_a_report():
	decision = await classify_intent(
		"Create a report listing customers",
		site_config={},
	)
	assert decision.intent == "create_report"
	assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_matches_build_a_report():
	decision = await classify_intent(
		"Build a report for top suppliers",
		site_config={},
	)
	assert decision.intent == "create_report"
	assert decision.source == "heuristic"


# ── Analytics guardrail (dev-side Insights short-circuit) ─────────


@pytest.mark.asyncio
async def test_analytics_prompt_short_circuits_to_unknown():
	# Regression: "Show top 10 customers by revenue this quarter" used to
	# hit the LLM classifier which mis-picked create_workflow (or any of
	# the 22 intents), and the Builder specialist then hallucinated a
	# full Workflow changeset. Dev-side guardrail must catch it even
	# when mode somehow landed on dev.
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(return_value="create_workflow"),
	) as llm:
		decision = await classify_intent(
			"Show top 10 customers by revenue this quarter", site_config={},
		)
		llm.assert_not_awaited()
		assert decision.intent == "unknown"
		assert decision.source == "analytics_guardrail"
		assert decision.confidence == "high"


@pytest.mark.asyncio
async def test_interrogative_prompt_short_circuits_to_unknown():
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(return_value="create_doctype"),
	) as llm:
		decision = await classify_intent(
			"What DocTypes do I have on this site?", site_config={},
		)
		llm.assert_not_awaited()
		assert decision.intent == "unknown"
		assert decision.source == "analytics_guardrail"


@pytest.mark.asyncio
async def test_list_my_prompt_short_circuits_to_unknown():
	with patch(
		"alfred.orchestrator._classify_intent_llm",
		new=AsyncMock(return_value="create_notification"),
	) as llm:
		decision = await classify_intent(
			"List my active notifications", site_config={},
		)
		llm.assert_not_awaited()
		assert decision.intent == "unknown"
		assert decision.source == "analytics_guardrail"


@pytest.mark.asyncio
async def test_build_verb_still_beats_analytics_shape():
	# Analytics guardrail must not swallow explicit build requests that
	# happen to mention analytics nouns. "Create a report for top X"
	# still routes to create_report via the heuristic pass.
	decision = await classify_intent(
		"Create a report listing top customers by revenue", site_config={},
	)
	assert decision.intent == "create_report"
	assert decision.source == "heuristic"
