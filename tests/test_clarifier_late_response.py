"""Tests for the clarifier timeout late-response acknowledgement path.

When a clarifier question times out (Future resolved with [TIMEOUT] after
900s), a late user response that arrives at 901s used to silently drop -
resolve_question() found no Future in _pending_questions and returned,
leaving the user confused about whether their message was received.

These tests exercise ConnectionState.resolve_question + ask_human in
isolation (no real WebSocket, no pipeline) to verify:
  1. On-time response: Future resolves, resolve_question returns True
  2. Late response: resolve_question returns False AND sends an info
     message back via the WebSocket mock
  3. Unknown msg_id (never asked): resolve_question returns False, does
     NOT send any info message (that would create a new false positive)
  4. Late-response ack fires exactly once - subsequent resolve_question
     calls with the same msg_id are silent
  5. GC window bounds the _expired_questions dict
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from alfred.api.websocket import _EXPIRED_Q_TTL_SECONDS, ConnectionState


def _make_conn() -> ConnectionState:
	ws = AsyncMock()
	conn = ConnectionState(
		websocket=ws, site_id="test", user="u@x", roles=[], site_config={},
	)
	return conn


@pytest.mark.asyncio
async def test_on_time_response_resolves_future():
	conn = _make_conn()

	async def asker():
		return await conn.ask_human("Pick a color", timeout=1)

	# Run the ask + late resolve in parallel; resolver wins.
	ask_task = asyncio.create_task(asker())
	# Let ask_human get past self._pending_questions[msg_id] = future
	await asyncio.sleep(0.01)
	assert len(conn._pending_questions) == 1
	msg_id = next(iter(conn._pending_questions))
	ok = await conn.resolve_question(msg_id, "blue")
	assert ok is True
	assert await ask_task == "blue"
	# Future cleanup: no leak
	assert len(conn._pending_questions) == 0
	assert msg_id not in conn._expired_questions


@pytest.mark.asyncio
async def test_late_response_sends_info_message():
	conn = _make_conn()

	# Short timeout so the test stays fast.
	answer = await conn.ask_human("Waited on user", timeout=0.05)
	assert answer.startswith("[TIMEOUT]")
	# After timeout, the msg_id is recorded as expired.
	assert len(conn._expired_questions) == 1
	msg_id = next(iter(conn._expired_questions))

	# The WS mock was used once for the question send; clear its call list.
	conn.websocket.send_json.reset_mock()

	# Late response arrives.
	ok = await conn.resolve_question(msg_id, "blue (late)")
	assert ok is False  # Future was already resolved via timeout
	# An info message was emitted back to the client.
	assert conn.websocket.send_json.await_count == 1
	sent = conn.websocket.send_json.await_args.args[0]
	assert sent["type"] == "info"
	assert sent["data"]["code"] == "CLARIFIER_LATE_RESPONSE"
	assert sent["data"]["response_to"] == msg_id
	# Entry was consumed so a second late response on the same msg_id
	# stays silent (avoids double-noise).
	assert msg_id not in conn._expired_questions


@pytest.mark.asyncio
async def test_unknown_msg_id_does_not_send_info():
	# resolve_question for an msg_id we never asked must NOT send an info
	# message - that would create a false positive for any malformed
	# frontend message carrying a garbage response_to.
	conn = _make_conn()
	ok = await conn.resolve_question("msg-never-asked", "whatever")
	assert ok is False
	assert conn.websocket.send_json.await_count == 0


@pytest.mark.asyncio
async def test_late_ack_fires_exactly_once():
	conn = _make_conn()
	await conn.ask_human("x", timeout=0.05)
	msg_id = next(iter(conn._expired_questions))
	conn.websocket.send_json.reset_mock()

	await conn.resolve_question(msg_id, "late 1")
	await conn.resolve_question(msg_id, "late 2")
	# First call emits an info message; second finds nothing and stays silent.
	assert conn.websocket.send_json.await_count == 1


@pytest.mark.asyncio
async def test_gc_drops_stale_entries():
	# Artificially age the expired entry past the TTL and check GC removes it.
	conn = _make_conn()
	conn._expired_questions["ancient"] = 0.0  # unix epoch - way past TTL
	conn._expired_questions["fresh"] = 99999999999.0  # far future - retained
	conn._gc_expired_questions()
	assert "ancient" not in conn._expired_questions
	assert "fresh" in conn._expired_questions


def test_ttl_constant_is_reasonable():
	# 1 hour is the documented window. If this constant ever shrinks below
	# 5 minutes or grows past a day, we probably did something accidental.
	assert 300 <= _EXPIRED_Q_TTL_SECONDS <= 86400
