"""Tests for graceful user-initiated cancel.

Covers the three hops of the cancel flow on the processing side:
  - `_send_error` routes `user_cancel` to a `run_cancelled` WS event (not `error`)
    and does NOT include the rescue path.
  - `_handle_custom_message` with `{"type": "cancel"}` and an active pipeline
    calls `ctx.stop(..., code="user_cancel")`.
  - `_handle_custom_message` with `{"type": "cancel"}` and NO active pipeline
    is a no-op (not an error).
  - `ctx.stop(code="user_cancel")` mid-run makes the pipeline exit at the
    next phase boundary and emits `run_cancelled` via `_send_error`.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from alfred.api.pipeline import AgentPipeline, PipelineContext
from alfred.api.websocket import _handle_custom_message


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


def _make_ctx(prompt: str = "build a notification") -> PipelineContext:
	conn = MagicMock()
	conn.send = AsyncMock()
	conn.site_id = "test-site"
	conn.user = "tester@example.com"
	conn.roles = ["System Manager"]
	conn.site_config = {"llm_model": "ollama/llama3.1", "pipeline_mode": "lite"}
	conn.mcp_client = None
	conn.websocket = MagicMock()
	conn.websocket.app.state.redis = None
	conn.websocket.app.state.settings = MagicMock(ADMIN_PORTAL_URL="", ADMIN_SERVICE_KEY="")
	return PipelineContext(conn=conn, conversation_id="conv-1", prompt=prompt)


class TestSendErrorRoutesUserCancel:
	def test_user_cancel_emits_run_cancelled_not_error(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)
		_run(pipeline._send_error("Cancelled by user", "user_cancel"))

		assert ctx.conn.send.call_count == 1
		payload = ctx.conn.send.call_args[0][0]
		assert payload["type"] == "run_cancelled"
		assert payload["data"]["reason"] == "Cancelled by user"
		assert "error" not in payload["data"]
		assert "code" not in payload["data"]

	def test_non_cancel_code_still_emits_error(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)
		_run(pipeline._send_error("Pipeline blew up", "PIPELINE_ERROR"))

		payload = ctx.conn.send.call_args[0][0]
		assert payload["type"] == "error"
		assert payload["data"]["code"] == "PIPELINE_ERROR"

	def test_user_cancel_tolerates_send_failure(self):
		ctx = _make_ctx()
		ctx.conn.send.side_effect = RuntimeError("socket closed")
		pipeline = AgentPipeline(ctx)
		# Must not raise - a closed socket during cancel is expected.
		_run(pipeline._send_error("Cancelled by user", "user_cancel"))


class TestPipelineShortCircuitOnUserCancel:
	def test_stop_user_cancel_mid_run_exits_with_run_cancelled(self):
		"""Mid-pipeline ctx.stop(code='user_cancel') breaks the PHASES loop
		at the next boundary and emits run_cancelled via _send_error."""
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		called: list[str] = []

		async def sanitize_then_cancel():
			called.append("sanitize")
			ctx.stop(error="Cancelled by user", code="user_cancel")

		async def must_not_run():
			called.append("load_state")

		with patch.object(pipeline, "_phase_sanitize", side_effect=sanitize_then_cancel), \
			 patch.object(pipeline, "_phase_load_state", side_effect=must_not_run):
			_run(pipeline.run())

		assert called == ["sanitize"]
		# Exactly one run_cancelled event, no error event.
		types = [c[0][0]["type"] for c in ctx.conn.send.call_args_list]
		assert "run_cancelled" in types
		assert "error" not in types


class TestHandleCustomMessageCancel:
	def _make_conn(self, with_ctx: bool = True):
		conn = MagicMock()
		conn.send = AsyncMock()
		conn.site_id = "test-site"
		conn.user = "tester@example.com"
		if with_ctx:
			# Simulate a pipeline mid-run: active_pipeline is a not-done Task,
			# active_pipeline_ctx exposes a real PipelineContext we can observe.
			conn.active_pipeline_ctx = _make_ctx()
			conn.active_pipeline = MagicMock()
			conn.active_pipeline.done.return_value = False
		else:
			conn.active_pipeline_ctx = None
			conn.active_pipeline = None
		return conn

	def test_cancel_with_active_pipeline_calls_stop(self):
		conn = self._make_conn(with_ctx=True)
		ctx = conn.active_pipeline_ctx
		websocket = MagicMock()

		_run(_handle_custom_message(
			{"type": "cancel", "msg_id": "m1"}, websocket, conn, "conv-1",
		))

		assert ctx.should_stop is True
		assert ctx.stop_signal is not None
		assert ctx.stop_signal.code == "user_cancel"

	def test_cancel_without_active_pipeline_is_noop(self):
		conn = self._make_conn(with_ctx=False)
		websocket = MagicMock()

		# Must not raise - user may click Stop after the pipeline has
		# already completed or before it ever started.
		_run(_handle_custom_message(
			{"type": "cancel", "msg_id": "m1"}, websocket, conn, "conv-1",
		))

	def test_cancel_after_pipeline_done_is_noop(self):
		conn = self._make_conn(with_ctx=True)
		conn.active_pipeline.done.return_value = True
		ctx = conn.active_pipeline_ctx
		websocket = MagicMock()

		_run(_handle_custom_message(
			{"type": "cancel", "msg_id": "m1"}, websocket, conn, "conv-1",
		))

		# stop() must NOT have been called since the task is already done.
		assert ctx.should_stop is False
