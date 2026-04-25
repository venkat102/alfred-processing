"""Tests for AgentPipeline._save_memory_with_feedback.

The helper is called at the end of every chat / insights / plan / dev
turn to persist conversation memory. Failures (Redis down, serialisation
glitch) used to crash the phase or silently drop the memory - now they
emit a non-blocking info WS event with code=MEMORY_SAVE_FAILED so the
user knows follow-up turns may be missing context.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext


def _make_pipeline(*, with_memory: bool = True, with_store: bool = True) -> AgentPipeline:
	conn = MagicMock()
	conn.site_id = "test.site"
	conn.user = "u@x"
	conn.site_config = {}
	conn.send = AsyncMock()

	ctx = PipelineContext(conn=conn, conversation_id="conv-1", prompt="test")
	ctx.mode = "dev"
	ctx.conversation_memory = MagicMock() if with_memory else None
	ctx.store = MagicMock() if with_store else None
	return AgentPipeline(ctx)


@pytest.mark.asyncio
async def test_no_op_when_memory_is_none():
	pipe = _make_pipeline(with_memory=False, with_store=True)
	# Patch at the import site - save_conversation_memory is imported inside
	# the helper so we need to patch the source module.
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(),
	) as save:
		await pipe._save_memory_with_feedback()
	save.assert_not_called()
	pipe.ctx.conn.send.assert_not_called()


@pytest.mark.asyncio
async def test_no_op_when_store_is_none():
	pipe = _make_pipeline(with_memory=True, with_store=False)
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(),
	) as save:
		await pipe._save_memory_with_feedback()
	save.assert_not_called()
	pipe.ctx.conn.send.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_saves_and_does_not_send_info():
	pipe = _make_pipeline()
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(),
	) as save:
		await pipe._save_memory_with_feedback()
	save.assert_awaited_once_with(
		pipe.ctx.store, "test.site", "conv-1", pipe.ctx.conversation_memory,
	)
	# No user-visible info event on the happy path.
	pipe.ctx.conn.send.assert_not_called()


@pytest.mark.asyncio
async def test_save_failure_emits_info_event():
	pipe = _make_pipeline()
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(side_effect=RuntimeError("redis unreachable")),
	):
		await pipe._save_memory_with_feedback()
	pipe.ctx.conn.send.assert_awaited_once()
	payload = pipe.ctx.conn.send.await_args.args[0]
	assert payload["type"] == "info"
	assert payload["data"]["code"] == "MEMORY_SAVE_FAILED"
	assert "conversation memory" in payload["data"]["message"].lower()
	assert payload["msg_id"]  # non-empty


@pytest.mark.asyncio
async def test_info_send_failure_does_not_raise():
	# If the WebSocket is also gone when we try to notify the user,
	# we can't do anything useful - swallow the send error, return
	# normally, let the caller proceed.
	pipe = _make_pipeline()
	pipe.ctx.conn.send = AsyncMock(side_effect=ConnectionError("ws closed"))
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(side_effect=RuntimeError("redis unreachable")),
	):
		# Must not raise.
		await pipe._save_memory_with_feedback()
	# Attempted the send (which failed).
	pipe.ctx.conn.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_info_payload_shape_matches_clarifier_pattern():
	# #FLOW2 established the `info` message contract: {type, data:
	# {message, code, ...}}. MEMORY_SAVE_FAILED must conform so the
	# client can render both codes through one handler.
	pipe = _make_pipeline()
	with patch(
		"alfred.state.conversation_memory.save_conversation_memory",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		await pipe._save_memory_with_feedback()
	payload = pipe.ctx.conn.send.await_args.args[0]
	assert set(payload.keys()) >= {"msg_id", "type", "data"}
	assert set(payload["data"].keys()) >= {"message", "code"}
