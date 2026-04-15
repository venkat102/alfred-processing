"""Tests for the chat mode handler.

Covers:
  - Successful LLM call returns the streamed reply
  - LLM failure returns the static fallback string (never raises)
  - Memory context is rendered into the system prompt
  - No MCP tools are passed to the LLM call
  - site_config LLM settings flow through to litellm kwargs
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from alfred.handlers.chat import handle_chat
from alfred.state.conversation_memory import ConversationMemory


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


class _FakeChunk:
	"""Shape-compatible with litellm chunk.choices[0].delta.content."""

	def __init__(self, token: str):
		self.choices = [MagicMock()]
		self.choices[0].delta.content = token


def _stream_of(*tokens):
	"""Build an iterable of fake chunks that yields the given tokens."""
	return iter([_FakeChunk(t) for t in tokens])


class TestHandleChat:
	def test_returns_llm_reply_on_success(self):
		def fake_completion(**kwargs):
			return _stream_of("Hello! ", "How can I help?")

		with patch("litellm.completion", side_effect=fake_completion):
			reply = _run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester", "roles": []},
					site_config={"llm_model": "ollama/llama3.1"},
				)
			)
		assert reply == "Hello! How can I help?"

	def test_empty_llm_reply_uses_fallback(self):
		def fake_completion(**kwargs):
			return _stream_of("", "   ")

		with patch("litellm.completion", side_effect=fake_completion):
			reply = _run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={},
				)
			)
		assert "Alfred" in reply
		assert "customization" in reply.lower()

	def test_llm_failure_returns_fallback(self):
		def boom(**kwargs):
			raise RuntimeError("llm down")

		with patch("litellm.completion", side_effect=boom):
			reply = _run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={},
				)
			)
		assert reply is not None
		assert "Alfred" in reply

	def test_memory_rendered_into_system_prompt(self):
		captured = {}

		def capture_completion(**kwargs):
			captured.update(kwargs)
			return _stream_of("reply")

		memory = ConversationMemory(conversation_id="c1")
		memory.add_changeset_items([
			{"op": "create", "doctype": "DocType", "data": {"name": "Book"}}
		])

		with patch("litellm.completion", side_effect=capture_completion):
			_run(
				handle_chat(
					prompt="summarize what we built",
					memory=memory,
					user_context={"user": "tester"},
					site_config={},
				)
			)

		system_msg = captured["messages"][0]["content"]
		assert "CONVERSATION CONTEXT" in system_msg
		assert "Book" in system_msg

	def test_memory_render_failure_still_returns_reply(self):
		class BrokenMemory:
			def render_for_prompt(self):
				raise RuntimeError("nope")

		def fake_completion(**kwargs):
			return _stream_of("still works")

		with patch("litellm.completion", side_effect=fake_completion):
			reply = _run(
				handle_chat(
					prompt="hi",
					memory=BrokenMemory(),
					user_context={"user": "tester"},
					site_config={},
				)
			)
		assert reply == "still works"

	def test_no_tools_in_kwargs(self):
		"""Chat mode must never pass tool schemas to the LLM call."""
		captured = {}

		def capture_completion(**kwargs):
			captured.update(kwargs)
			return _stream_of("reply")

		with patch("litellm.completion", side_effect=capture_completion):
			_run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={},
				)
			)

		assert "tools" not in captured
		assert "functions" not in captured
		assert "tool_choice" not in captured

	def test_site_config_flows_to_kwargs(self):
		captured = {}

		def capture_completion(**kwargs):
			captured.update(kwargs)
			return _stream_of("reply")

		with patch("litellm.completion", side_effect=capture_completion):
			_run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={
						"llm_model": "anthropic/claude-3-5-haiku",
						"llm_api_key": "sk-test",
						"llm_base_url": "https://api.test/v1",
					},
				)
			)

		assert captured["model"] == "anthropic/claude-3-5-haiku"
		assert captured["api_key"] == "sk-test"
		assert captured["base_url"] == "https://api.test/v1"

	def test_uses_low_temperature_and_small_max_tokens(self):
		captured = {}

		def capture_completion(**kwargs):
			captured.update(kwargs)
			return _stream_of("reply")

		with patch("litellm.completion", side_effect=capture_completion):
			_run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={},
				)
			)

		# Chat replies should be short and not wildly creative
		assert captured["max_tokens"] <= 512
		assert captured["temperature"] <= 0.5
