"""Safe-default fallbacks when the LLM is unreachable or malformed.

Every caller of ollama_chat / ollama_chat_sync must degrade gracefully
when the Ollama server goes down, throws a protocol error, or returns
garbage. If any of these raises uncaught, the user sees a 500 and a
pipeline stuck mid-flight. These tests pin the contract.

Covers:
- orchestrator._classify_with_llm -> (None, "", "low") on any error
- handlers.chat.handle_chat -> generic ack string, no raise
- agents.prompt_enhancer.enhance_prompt -> returns raw prompt unchanged
- agents.reflection.reflect_minimality -> returns changeset untouched,
  removed_list empty
- api.websocket._clarify_requirements -> (enhanced_prompt, [])
- api.websocket._rescue_regenerate_changeset -> []
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.llm_client import OllamaError


def _run(coro):
	return asyncio.new_event_loop().run_until_complete(coro)


SITE_CONFIG = {"llm_model": "ollama/test", "llm_base_url": "http://localhost:11434"}


class TestClassifierFallback:
	def test_classifier_returns_low_on_ollama_error(self):
		from alfred.orchestrator import _classify_with_llm

		async def boom(*args, **kwargs):
			raise OllamaError("ollama is down")

		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			mode, reason, confidence = _run(_classify_with_llm("prompt", "", SITE_CONFIG))
		assert mode is None
		assert confidence == "low"

	def test_classifier_returns_low_on_timeout(self):
		from alfred.orchestrator import _classify_with_llm

		async def boom(*args, **kwargs):
			raise TimeoutError("read timed out")

		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			mode, _, confidence = _run(_classify_with_llm("p", "", SITE_CONFIG))
		assert mode is None
		assert confidence == "low"


class TestChatHandlerFallback:
	def test_chat_handler_returns_generic_string_on_error(self):
		from alfred.handlers.chat import handle_chat

		async def boom(*args, **kwargs):
			raise OllamaError("ollama is down")

		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			reply = _run(handle_chat(
				"hi",
				memory=None,
				user_context={"user": "u@x.com", "roles": [], "site_id": "s"},
				site_config=SITE_CONFIG,
			))
		assert isinstance(reply, str)
		assert len(reply) > 0
		assert "Alfred" in reply

	def test_chat_handler_falls_back_on_empty_reply(self):
		from alfred.handlers.chat import handle_chat

		async def returns_whitespace(*args, **kwargs):
			return "   \n\t  "

		with patch("alfred.llm_client.ollama_chat", side_effect=returns_whitespace):
			reply = _run(handle_chat(
				"hi",
				memory=None,
				user_context={"user": "u@x.com", "roles": [], "site_id": "s"},
				site_config=SITE_CONFIG,
			))
		# Whitespace-only reply should trigger the fallback
		assert "Alfred" in reply


class TestEnhancerFallback:
	def test_enhancer_returns_raw_prompt_on_error(self):
		from alfred.agents.prompt_enhancer import enhance_prompt

		async def boom(*args, **kwargs):
			raise OllamaError("boom")

		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			out = _run(enhance_prompt(
				raw_prompt="add a field",
				user_context={"user": "u", "roles": ["System Manager"]},
				conversation_context="",
				site_config=SITE_CONFIG,
			))
		assert out == "add a field"


class TestReflectionFallback:
	def test_reflection_returns_changeset_unchanged_on_error(self):
		from alfred.agents.reflection import reflect_minimality

		changeset = [{"op": "create", "doctype": "Custom Field", "data": {"name": "x"}}]

		async def boom(*args, **kwargs):
			raise OllamaError("boom")

		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			kept, removed = _run(reflect_minimality(
				changeset=changeset,
				original_prompt="add a field",
				site_config=SITE_CONFIG,
			))
		assert kept == changeset
		assert removed == []

	def test_reflection_returns_changeset_unchanged_on_garbage(self):
		from alfred.agents.reflection import reflect_minimality

		changeset = [{"op": "create", "doctype": "Custom Field", "data": {"name": "x"}}]

		async def garbage(*args, **kwargs):
			return "not json at all"

		with patch("alfred.llm_client.ollama_chat", side_effect=garbage):
			kept, removed = _run(reflect_minimality(
				changeset=changeset,
				original_prompt="add a field",
				site_config=SITE_CONFIG,
			))
		assert kept == changeset
		assert removed == []


class TestClarifierFallback:
	def _make_conn(self):
		conn = MagicMock()
		conn.send = AsyncMock()
		conn.ask_human = AsyncMock(return_value="")
		conn.site_config = SITE_CONFIG
		return conn

	def test_clarifier_returns_original_prompt_on_error(self):
		from alfred.api.websocket import _clarify_requirements

		async def boom(*args, **kwargs):
			raise OllamaError("boom")

		event_cb = AsyncMock()
		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			prompt_out, qa_out = _run(_clarify_requirements(
				enhanced_prompt="do the thing",
				conn=self._make_conn(),
				event_callback=event_cb,
			))
		assert prompt_out == "do the thing"
		assert qa_out == []

	def test_clarifier_returns_original_prompt_on_garbage_json(self):
		from alfred.api.websocket import _clarify_requirements

		async def garbage(*args, **kwargs):
			return "definitely not a JSON array"

		event_cb = AsyncMock()
		with patch("alfred.llm_client.ollama_chat", side_effect=garbage):
			prompt_out, qa_out = _run(_clarify_requirements(
				enhanced_prompt="do the thing",
				conn=self._make_conn(),
				event_callback=event_cb,
			))
		assert prompt_out == "do the thing"
		assert qa_out == []


class TestRescueFallback:
	def test_rescue_returns_empty_list_on_error(self):
		from alfred.api.websocket import _rescue_regenerate_changeset

		async def boom(*args, **kwargs):
			raise OllamaError("boom")

		event_cb = AsyncMock()
		with patch("alfred.llm_client.ollama_chat", side_effect=boom):
			result = _run(_rescue_regenerate_changeset(
				original_prompt="build a notification",
				failed_output="blah",
				site_config=SITE_CONFIG,
				event_callback=event_cb,
				user_prompt="build a notification",
			))
		assert result == []

	def test_rescue_returns_empty_list_on_garbage(self):
		from alfred.api.websocket import _rescue_regenerate_changeset

		async def garbage(*args, **kwargs):
			return "I cannot regenerate"

		event_cb = AsyncMock()
		with patch("alfred.llm_client.ollama_chat", side_effect=garbage):
			result = _run(_rescue_regenerate_changeset(
				original_prompt="build a notification",
				failed_output="blah",
				site_config=SITE_CONFIG,
				event_callback=event_cb,
				user_prompt="build a notification",
			))
		assert result == []
