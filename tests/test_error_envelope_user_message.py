"""Pin the M3 wire: ``_send_error`` enriches every error event with a
``user_message`` field translated from the pipeline's UPPER_SNAKE
code.

Before this wire landed, ``alfred.middleware.error_handling`` defined
``get_user_error_message`` and ``ERROR_MESSAGES`` but no production
code path called them — the audit's M3.

The frontend stays free to ignore ``user_message`` and render the raw
``error`` text; the field is purely additive."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.api.pipeline.context import PipelineContext
from alfred.api.pipeline.runner import AgentPipeline


def _make_ctx() -> PipelineContext:
	conn = MagicMock()
	conn.send = AsyncMock()
	conn.site_id = "site-a"
	conn.site_config = {}
	ctx = PipelineContext(conn=conn, conversation_id="conv-x", prompt="p")
	return ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
	("code", "expected_substring"),
	[
		# Each row pins one (pipeline-emitted code) → (user-facing
		# fragment) mapping. If a future translator change drops one
		# of these, the contract test here surfaces the regression.
		("OLLAMA_UNHEALTHY", "AI service is temporarily unavailable"),
		("PIPELINE_TIMEOUT", "taking too long"),
		("PROMPT_BLOCKED", "flagged by our security filter"),
		("RATE_LIMIT", "maximum number of requests"),
		("REDIS_UNAVAILABLE", "Internal state service is unavailable"),
	],
)
async def test_send_error_attaches_user_message_for_known_code(code, expected_substring):
	ctx = _make_ctx()
	pipeline = AgentPipeline(ctx)

	await pipeline._send_error("technical detail", code)

	sent = ctx.conn.send.await_args_list[0].args[0]
	assert sent["type"] == "error"
	# Raw fields the UI may already depend on stay intact.
	assert sent["data"]["code"] == code
	assert sent["data"]["error"] == "technical detail"
	# New field — the wire under test.
	assert expected_substring in sent["data"]["user_message"]


@pytest.mark.asyncio
async def test_send_error_falls_back_to_unknown_for_new_code():
	"""A code the translator hasn't seen yet must NOT crash the send.
	Falls back to the generic 'unexpected error' string so the user
	gets *something* meaningful while we add the mapping."""
	ctx = _make_ctx()
	pipeline = AgentPipeline(ctx)

	await pipeline._send_error("oh no", "BRAND_NEW_CODE_2099")

	sent = ctx.conn.send.await_args_list[0].args[0]
	assert sent["data"]["code"] == "BRAND_NEW_CODE_2099"
	assert "unexpected error" in sent["data"]["user_message"].lower()


@pytest.mark.asyncio
async def test_send_error_user_cancel_path_unchanged():
	"""``user_cancel`` takes a different envelope (``run_cancelled``,
	not ``error``). The new ``user_message`` enrichment must not
	pollute that path or downgrade it to a generic error."""
	ctx = _make_ctx()
	pipeline = AgentPipeline(ctx)

	await pipeline._send_error("user cancelled", "user_cancel")

	sent = ctx.conn.send.await_args_list[0].args[0]
	assert sent["type"] == "run_cancelled"
	# No user_message key is added to the cancellation envelope —
	# UX treats cancellations as informational, not errors.
	assert "user_message" not in sent["data"]
