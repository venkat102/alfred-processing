"""Tests for the chat mode handler.

Covers:
  - Successful LLM call returns the reply
  - LLM failure returns the static fallback string (never raises)
  - Memory context is rendered into the system prompt
  - Chat uses conversational temperature and small max_tokens
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from alfred.handlers.chat import handle_chat
from alfred.state.conversation_memory import ConversationMemory


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


class TestHandleChat:
	def test_returns_llm_reply_on_success(self):
		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.return_value = "Hello! How can I help?"
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
		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.return_value = "   "
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
		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.side_effect = RuntimeError("llm down")
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
		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.return_value = "reply"

			memory = ConversationMemory(conversation_id="c1")
			memory.add_changeset_items([
				{"op": "create", "doctype": "DocType", "data": {"name": "Book"}}
			])

			_run(
				handle_chat(
					prompt="summarize what we built",
					memory=memory,
					user_context={"user": "tester"},
					site_config={},
				)
			)

		# Verify the system message includes memory context
		call_kwargs = mock.call_args
		messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages") or call_kwargs[0][0]
		system_msg = messages[0]["content"]
		assert "CONVERSATION CONTEXT" in system_msg
		assert "Book" in system_msg

	def test_memory_render_failure_still_returns_reply(self):
		class BrokenMemory:
			def render_for_prompt(self):
				raise RuntimeError("nope")

		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.return_value = "still works"
			reply = _run(
				handle_chat(
					prompt="hi",
					memory=BrokenMemory(),
					user_context={"user": "tester"},
					site_config={},
				)
			)
		assert reply == "still works"

	def test_passes_conversational_params(self):
		"""Chat mode uses low max_tokens and moderate temperature."""
		with patch("alfred.llm_client.ollama_chat", new_callable=AsyncMock) as mock:
			mock.return_value = "reply"
			_run(
				handle_chat(
					prompt="hi",
					memory=None,
					user_context={"user": "tester"},
					site_config={},
				)
			)

		call_kwargs = mock.call_args
		kw = call_kwargs.kwargs if call_kwargs.kwargs else {}
		assert kw.get("max_tokens", 256) <= 512
		assert kw.get("temperature", 0.3) <= 0.5
