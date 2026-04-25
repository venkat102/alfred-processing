"""Demonstrate ``UserInteractionHandler``'s adapter-context wire.

The audit's M4 noted that ``UserInteractionHandler`` and
``build_ask_user_tool`` had no production callers — the WebSocket
path uses ``ConnectionState.ask_human`` instead. The decision was to
**keep** the module as documented adapter infrastructure rather than
delete it (see the module docstring), since it remains the right
choice for adapter contexts that don't have a ``ConnectionState``.

This file pins one such adapter pattern as a working example so a
future reader sees a concrete demo of how to use the module.
"""

from __future__ import annotations

import asyncio

import pytest

from alfred.tools.user_interaction import UserInteractionHandler


class _FakeRestSink:
	"""Stand-in for a REST-style transport that just records messages.

	A real REST flow would push the message into a Redis stream the
	client polls; here we just collect them for the assertion."""

	def __init__(self) -> None:
		self.messages: list[dict] = []

	async def send(self, message: dict) -> None:
		self.messages.append(message)


@pytest.mark.asyncio
async def test_handler_drives_ask_response_cycle_via_send_func():
	"""Adapter context: caller wires a custom ``send`` into the
	handler, runs ``ask_user``, then routes the user's response back
	in via ``handle_user_response``. No WebSocket required."""
	sink = _FakeRestSink()
	handler = UserInteractionHandler(sink.send, timeout=5)

	async def _respond_after_send():
		# Wait until the question lands in the sink, then forward
		# a user_response frame the same shape the WS router uses.
		while not sink.messages:
			await asyncio.sleep(0.01)
		question = sink.messages[-1]
		handler.handle_user_response({
			"msg_id": question["msg_id"],
			"type": "user_response",
			"data": {
				"text": "yes please",
				"response_to": question["msg_id"],
			},
		})

	responder = asyncio.create_task(_respond_after_send())
	answer = await handler.ask_user("Add a workflow?", ["yes please", "no"])
	await responder

	# The handler shipped a question with the documented envelope shape …
	assert sink.messages[0]["type"] == "question"
	assert sink.messages[0]["data"]["question"] == "Add a workflow?"
	assert sink.messages[0]["data"]["choices"] == ["yes please", "no"]
	# … and the awaited answer was the text the responder forwarded.
	assert answer == "yes please"
	# Pending state is released after the response — no leaks.
	assert handler.pending_count == 0


@pytest.mark.asyncio
async def test_build_ask_user_tool_wraps_handler_into_a_crewai_tool():
	"""``build_ask_user_tool`` lifts the handler into a CrewAI ``@tool``
	function so an agent could call it from synchronous tool-execution
	context. The wrapper is the contract; pin its surface."""
	from alfred.tools.user_interaction import build_ask_user_tool

	sink = _FakeRestSink()
	handler = UserInteractionHandler(sink.send, timeout=2)

	tool_fn = build_ask_user_tool(handler)
	# CrewAI tools expose .name and .description for agent prompts.
	assert getattr(tool_fn, "name", None) == "ask_user"
	# The wrapped function is what the agent invokes — CrewAI Tool
	# objects expose ``.func`` for the underlying callable. Both the
	# Tool itself and its underlying ``func`` are valid invocation
	# surfaces depending on CrewAI version.
	inner_func = getattr(tool_fn, "func", None)
	assert inner_func is not None and callable(inner_func)
