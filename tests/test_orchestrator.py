"""Tests for the three-mode chat orchestrator.

Covers:
  - Manual override: non-"auto" values bypass LLM and fast-path
  - Fast-path: exact greetings -> chat, build verbs -> dev
  - LLM classification: mock litellm, assert mode comes through
  - Low-confidence fallback: picks chat when no active plan, dev otherwise
  - Parse resilience: fenced JSON, extra prose around JSON, bad JSON
  - Empty prompt -> chat
  - Normalization of override strings
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from alfred.orchestrator import (
	_clip_memory_context,
	_fast_path,
	_normalize_mode,
	_normalize_override,
	_parse_classifier_output,
	classify_mode,
	is_enabled,
)
from alfred.state.conversation_memory import ConversationMemory


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


class TestNormalization:
	def test_override_none_returns_auto(self):
		assert _normalize_override(None) == "auto"

	def test_override_empty_returns_auto(self):
		assert _normalize_override("") == "auto"

	def test_override_case_insensitive(self):
		assert _normalize_override("DEV") == "dev"
		assert _normalize_override("Plan") == "plan"

	def test_override_invalid_returns_auto(self):
		assert _normalize_override("banana") == "auto"

	def test_mode_case_insensitive(self):
		assert _normalize_mode("DEV") == "dev"

	def test_mode_invalid_returns_none(self):
		assert _normalize_mode("banana") is None
		assert _normalize_mode("") is None
		assert _normalize_mode(None) is None


class TestFastPath:
	def test_empty_prompt_returns_chat(self):
		assert _fast_path("") == "chat"
		assert _fast_path("   ") == "chat"

	def test_greetings_hit_chat(self):
		for g in ["hi", "Hello", "hey!", "thanks", "Thank you.", "ok", "bye"]:
			assert _fast_path(g) == "chat", f"failed on {g!r}"

	def test_build_verbs_hit_dev(self):
		for p in [
			"add a priority field to Sales Order",
			"Create a DocType called Book",
			"build a notification for leave approval",
			"Make an approval workflow",
		]:
			assert _fast_path(p) == "dev", f"failed on {p!r}"

	def test_ambiguous_returns_none(self):
		"""Prompts that don't match any fast-path prefix go through the LLM.

		Note: 'what DocTypes do I have?' used to be ambiguous but now
		fast-paths to insights (Phase B). 'how would we approach...' is
		still ambiguous - it's a plan question but the fast-path doesn't
		cover plan mode, so it falls through to the LLM classifier.
		"""
		assert _fast_path("how would we approach adding approval?") is None
		assert _fast_path("should we use a workflow or a server script here?") is None
		assert _fast_path("xyzzy random gibberish") is None

	def test_greeting_with_punctuation(self):
		assert _fast_path("Hi!") == "chat"
		assert _fast_path("thanks!!!") == "chat"

	def test_greeting_prefix_not_exact_falls_through(self):
		"""'hi there' starts with a greeting but isn't an exact match.

		Deliberately kept narrow - anything that's not clearly a bare
		greeting goes through the LLM so the classifier can read the rest
		of the sentence.
		"""
		assert _fast_path("hi there, can you help?") is None

	def test_insights_interrogative_prefixes(self):
		"""Phase B: common read-only query phrasings should fast-path to insights."""
		insights_prompts = [
			"what DocTypes do I have?",
			"which workflows are active on Leave Application?",
			"show me my notifications",
			"list my custom fields",
			"how many modules are installed?",
			"do I have any workflows for Expense Claim?",
		]
		for p in insights_prompts:
			assert _fast_path(p) == "insights", f"failed on {p!r}"

	def test_insights_substring_patterns(self):
		"""Substring matches like 'do I have' should also land on insights."""
		assert _fast_path("are there any notifications on Sales Order I should know about? do i have any?") == "insights"

	def test_insights_does_not_misfire_on_dev_verbs(self):
		"""'Add a custom field' is dev, not insights - build verbs win."""
		# The orchestrator walks dev prefixes first, so "add a X" wins
		assert _fast_path("add a custom field to my DocType") == "dev"


class TestParseClassifierOutput:
	def test_clean_json(self):
		mode, reason, conf = _parse_classifier_output(
			'{"mode": "dev", "reason": "build verb", "confidence": "high"}'
		)
		assert mode == "dev"
		assert reason == "build verb"
		assert conf == "high"

	def test_fenced_json(self):
		text = '```json\n{"mode": "chat", "reason": "hi", "confidence": "high"}\n```'
		mode, reason, conf = _parse_classifier_output(text)
		assert mode == "chat"

	def test_prose_around_json(self):
		text = (
			"Looking at this, I think:\n"
			'{"mode": "plan", "reason": "design question", "confidence": "medium"}'
		)
		mode, reason, conf = _parse_classifier_output(text)
		assert mode == "plan"
		assert conf == "medium"

	def test_invalid_mode_returns_none(self):
		mode, _, _ = _parse_classifier_output(
			'{"mode": "banana", "reason": "", "confidence": "high"}'
		)
		assert mode is None

	def test_missing_confidence_defaults_medium(self):
		mode, _, conf = _parse_classifier_output('{"mode": "dev", "reason": ""}')
		assert mode == "dev"
		assert conf == "medium"

	def test_empty_string_returns_none(self):
		mode, _, _ = _parse_classifier_output("")
		assert mode is None

	def test_garbage_returns_none(self):
		mode, _, _ = _parse_classifier_output("this is not json at all")
		assert mode is None

	def test_json_with_extra_fields_still_parses(self):
		text = (
			'{"mode": "dev", "reason": "build", "confidence": "high", '
			'"extra_noise": "ignored", "telemetry": {"k": 1}}'
		)
		mode, reason, conf = _parse_classifier_output(text)
		assert mode == "dev"
		assert conf == "high"

	def test_mixed_case_mode_normalized(self):
		mode, _, _ = _parse_classifier_output(
			'{"mode": "DEV", "reason": "ok", "confidence": "high"}'
		)
		assert mode == "dev"

	def test_array_returns_none(self):
		# Top-level array, not object - must not crash.
		mode, _, _ = _parse_classifier_output('[{"mode": "dev"}]')
		assert mode is None

	def test_huge_response_with_embedded_json(self):
		# A verbose local model may produce prose before AND after the JSON.
		text = (
			"Here is a detailed analysis of the prompt:\n\n" * 50
			+ '{"mode": "insights", "reason": "user asks about site", "confidence": "high"}\n'
			+ "That's my final answer.\n" * 20
		)
		mode, _, conf = _parse_classifier_output(text)
		assert mode == "insights"
		assert conf == "high"

	def test_confidence_with_surrounding_whitespace(self):
		mode, _, conf = _parse_classifier_output(
			'{"mode": "dev", "reason": "", "confidence": "  HIGH  "}'
		)
		assert conf == "high"

	def test_null_fields_fall_back_cleanly(self):
		# Some models emit null instead of omitting keys.
		mode, reason, conf = _parse_classifier_output(
			'{"mode": "dev", "reason": null, "confidence": null}'
		)
		assert mode == "dev"
		assert reason == ""
		assert conf == "medium"

	def test_garbage_logs_warning_not_silent(self, caplog):
		"""The (None, "", "low") fallback must log so prompt regressions are visible.

		Regression guard for commit c124f9b: before it, the except blocks
		at orchestrator.py:293 + :299 silently returned without logging,
		and classifier drift could land as "low confidence" routing for
		weeks before anyone noticed.
		"""
		import logging
		with caplog.at_level(logging.WARNING, logger="alfred.orchestrator"):
			_parse_classifier_output("this is not json at all")
		matching = [
			r for r in caplog.records
			if "mode classifier JSON parse failed" in r.getMessage()
		]
		assert matching, "expected a WARNING log on JSON parse failure"


class TestClassifyMode:
	def test_manual_override_bypasses_llm(self):
		with patch("alfred.orchestrator._classify_with_llm") as llm:
			decision = _run(
				classify_mode(
					prompt="whatever",
					memory=None,
					manual_override="dev",
					site_config={},
				)
			)
		assert decision.mode == "dev"
		assert decision.source == "override"
		llm.assert_not_called()

	def test_manual_override_normalizes_case(self):
		decision = _run(
			classify_mode(
				prompt="whatever",
				memory=None,
				manual_override="PLAN",
				site_config={},
			)
		)
		assert decision.mode == "plan"

	def test_manual_override_invalid_falls_through_to_fast_path(self):
		decision = _run(
			classify_mode(
				prompt="hi",
				memory=None,
				manual_override="banana",
				site_config={},
			)
		)
		# Invalid override -> normalize to auto -> fast-path -> chat
		assert decision.mode == "chat"
		assert decision.source == "fast_path"

	def test_fast_path_greeting_skips_llm(self):
		with patch("alfred.orchestrator._classify_with_llm") as llm:
			decision = _run(
				classify_mode(
					prompt="hello",
					memory=None,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "chat"
		assert decision.source == "fast_path"
		llm.assert_not_called()

	def test_fast_path_build_verb_skips_llm(self):
		with patch("alfred.orchestrator._classify_with_llm") as llm:
			decision = _run(
				classify_mode(
					prompt="add a priority field to Sales Order",
					memory=None,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "dev"
		assert decision.source == "fast_path"
		llm.assert_not_called()

	def test_llm_classification_high_confidence(self):
		async def fake_llm(prompt, memory_context, site_config):
			return "plan", "design question", "high"

		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="how would we approach adding approval?",
					memory=None,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "plan"
		assert decision.source == "classifier"
		assert decision.confidence == "high"

	def test_low_confidence_falls_back_to_chat_without_active_plan(self):
		async def fake_llm(prompt, memory_context, site_config):
			return "plan", "not sure", "low"

		memory = ConversationMemory(conversation_id="c1")
		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="xyzzy",
					memory=memory,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "chat"
		assert decision.source == "fallback"

	def test_low_confidence_falls_back_to_dev_with_active_plan(self):
		async def fake_llm(prompt, memory_context, site_config):
			return "plan", "not sure", "low"

		memory = ConversationMemory(conversation_id="c1")
		# Simulate an active plan being present (forward-compat with Phase C)
		memory.active_plan = {"title": "Some plan"}

		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="xyzzy",
					memory=memory,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "dev"
		assert decision.source == "fallback"

	def test_classifier_unavailable_falls_back(self):
		async def fake_llm(prompt, memory_context, site_config):
			return None, "", "low"

		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="xyzzy gibberish",
					memory=None,
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "chat"
		assert decision.source == "fallback"
		assert "unavailable" in decision.reason

	def test_memory_render_failure_does_not_crash(self):
		async def fake_llm(prompt, memory_context, site_config):
			return "dev", "ok", "high"

		class BrokenMemory:
			def render_for_prompt(self):
				raise RuntimeError("oops")

		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="random text",
					memory=BrokenMemory(),
					manual_override="auto",
					site_config={},
				)
			)
		assert decision.mode == "dev"
		assert decision.source == "classifier"

	def test_never_raises_on_complete_failure(self):
		"""Even with every subsystem broken, classify_mode returns a valid decision."""
		async def fake_llm(*a, **kw):
			raise RuntimeError("boom")

		with patch("alfred.orchestrator._classify_with_llm", side_effect=fake_llm):
			decision = _run(
				classify_mode(
					prompt="something ambiguous",
					memory=None,
					manual_override="auto",
					site_config={},
				)
			)
		# LLM raised but classify_mode caught it via its own try/except,
		# so the result is the fallback chat.
		assert decision.mode == "chat"
		assert decision.source == "fallback"


class TestIsEnabled:
	def test_unset_is_disabled(self, monkeypatch):
		monkeypatch.delenv("ALFRED_ORCHESTRATOR_ENABLED", raising=False)
		assert is_enabled() is False

	def test_explicit_zero_is_disabled(self, monkeypatch):
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "0")
		assert is_enabled() is False

	def test_accepts_common_truthy_strings(self, monkeypatch):
		for val in ("1", "true", "TRUE", "Yes", "on", "  true  "):
			monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", val)
			assert is_enabled() is True, f"expected {val!r} to enable"

	def test_rejects_garbage(self, monkeypatch):
		for val in ("maybe", "0", "false", ""):
			monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", val)
			assert is_enabled() is False, f"expected {val!r} to stay off"


class TestClipMemoryContext:
	def test_short_context_passes_through(self):
		text = "short context"
		assert _clip_memory_context(text) == text

	def test_long_context_clipped_with_marker(self):
		text = "a" * 5000
		result = _clip_memory_context(text, cap=1000)
		assert result.startswith("[... older context clipped ...]")
		# Tail preserved: last 1000 chars of original must still be present.
		assert result.endswith("a" * 1000)
		# Total is the marker + the 1000-char tail.
		assert len(result) == len("[... older context clipped ...]\n") + 1000

	def test_exactly_at_cap_not_clipped(self):
		text = "b" * 1000
		result = _clip_memory_context(text, cap=1000)
		assert result == text
		assert "clipped" not in result
