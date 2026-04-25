"""Unit-level tests for #SEC3 (WebSocket prompt rate limiting).

Complements the integration-style WS tests in test_api_gateway.py which
require httpx_ws and get skipped in most local environments. These
tests construct a ConnectionState and call _handle_custom_message
directly, so they run everywhere.

Verifies:
  - RATE_LIMIT error is emitted with retry_after + limit when
    check_rate_limit returns False
  - pipeline is NOT started when rate limit denies
  - clarifier-answer fast-path bypasses rate limit (answering a
    pending question is a continuation, not a new task)
  - default max_per_hour falls back to DEFAULT_MAX_TASKS_PER_HOUR
    when site_config omits the field
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.api.websocket import ConnectionState, _handle_custom_message


def _make_conn(max_per_hour: int | None = None) -> ConnectionState:
	ws = MagicMock()
	ws.send_json = AsyncMock()
	# Mock the app.state.redis chain that _handle_custom_message reads.
	# A bare MagicMock for app.state would make every attribute access
	# return a truthy MagicMock — including the new ``shutting_down``
	# flag the P0.1 wire reads. Explicit defaults make the tests run
	# in the "service is up" state the rate-limit logic was written for.
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.redis = MagicMock()  # Presence is what matters; check_rate_limit is patched
	ws.app.state.shutting_down = False
	ws.app.state.active_pipelines = 0

	site_config: dict = {}
	if max_per_hour is not None:
		site_config["max_tasks_per_user_per_hour"] = max_per_hour

	conn = ConnectionState(
		websocket=ws, site_id="t.site", user="u@x",
		roles=["System Manager"], site_config=site_config,
	)
	return conn


@pytest.mark.asyncio
async def test_rate_limit_rejects_with_expected_payload():
	conn = _make_conn(max_per_hour=7)
	prompt_msg = {
		"msg_id": "p1",
		"type": "prompt",
		"data": {"text": "Create a Book DocType"},
	}

	with patch(
		"alfred.middleware.rate_limit.check_rate_limit",
		new=AsyncMock(return_value=(False, 0, 99)),
	), patch(
		"alfred.api.websocket._run_agent_pipeline",
		new=AsyncMock(return_value=None),
	) as run_pipeline:
		await _handle_custom_message(prompt_msg, conn.websocket, conn, "conv-1")

	# Pipeline MUST NOT start when rate-limited.
	run_pipeline.assert_not_called()

	# Rate-limit error went back with the documented shape.
	conn.websocket.send_json.assert_awaited_once()
	payload = conn.websocket.send_json.await_args.args[0]
	assert payload["type"] == "error"
	assert payload["data"]["code"] == "RATE_LIMIT"
	assert payload["data"]["retry_after"] == 99
	assert payload["data"]["limit"] == 7
	assert "7/hour" in payload["data"]["error"]


@pytest.mark.asyncio
async def test_rate_limit_allows_and_schedules_pipeline():
	conn = _make_conn(max_per_hour=20)
	prompt_msg = {
		"msg_id": "p1",
		"type": "prompt",
		"data": {"text": "ok"},
	}

	with patch(
		"alfred.middleware.rate_limit.check_rate_limit",
		new=AsyncMock(return_value=(True, 19, 0)),
	), patch(
		"alfred.api.websocket._run_agent_pipeline",
		new=AsyncMock(return_value=None),
	) as run_pipeline:
		await _handle_custom_message(prompt_msg, conn.websocket, conn, "conv-1")
		# The pipeline is scheduled as a Task; give it a tick.
		import asyncio as _asyncio
		await _asyncio.sleep(0)
		if conn.active_pipeline is not None:
			await conn.active_pipeline

	# No RATE_LIMIT error emitted.
	calls = conn.websocket.send_json.await_args_list
	for call in calls:
		payload = call.args[0]
		if payload.get("type") == "error":
			assert payload["data"].get("code") != "RATE_LIMIT"
	# Pipeline was invoked.
	assert run_pipeline.await_count == 1


@pytest.mark.asyncio
async def test_clarifier_answer_bypasses_rate_limit():
	"""When _pending_questions is non-empty the prompt is routed as an
	answer. That path must NOT call check_rate_limit - answering a
	clarifier is a continuation, not a new task."""
	conn = _make_conn(max_per_hour=1)
	# Seed a pending question so the prompt gets routed as an answer.
	import asyncio
	fut: asyncio.Future = asyncio.get_event_loop().create_future()
	conn._pending_questions["waiting-q"] = fut

	prompt_msg = {
		"msg_id": "p1",
		"type": "prompt",
		"data": {"text": "blue"},
	}

	with patch(
		"alfred.middleware.rate_limit.check_rate_limit",
		new=AsyncMock(return_value=(False, 0, 60)),
	) as check, patch(
		"alfred.api.websocket._run_agent_pipeline",
		new=AsyncMock(return_value=None),
	) as run_pipeline:
		await _handle_custom_message(prompt_msg, conn.websocket, conn, "conv-1")

	# Rate-limit check was NEVER called.
	check.assert_not_called()
	# Pipeline was NOT started (the prompt became an answer).
	run_pipeline.assert_not_called()
	# The pending Future was resolved with the user's text.
	assert fut.done()
	assert fut.result() == "blue"


@pytest.mark.asyncio
async def test_default_max_per_hour_applied_when_site_config_missing_key():
	from alfred.middleware.rate_limit import DEFAULT_MAX_TASKS_PER_HOUR

	conn = _make_conn(max_per_hour=None)  # no override in site_config
	prompt_msg = {
		"msg_id": "p1",
		"type": "prompt",
		"data": {"text": "ok"},
	}

	with patch(
		"alfred.middleware.rate_limit.check_rate_limit",
		new=AsyncMock(return_value=(True, 10, 0)),
	) as check, patch(
		"alfred.api.websocket._run_agent_pipeline",
		new=AsyncMock(return_value=None),
	):
		await _handle_custom_message(prompt_msg, conn.websocket, conn, "conv-1")
		import asyncio as _asyncio
		await _asyncio.sleep(0)
		if conn.active_pipeline is not None:
			await conn.active_pipeline

	# Called with the default, not a stray None or 0.
	check.assert_awaited_once()
	kwargs = check.await_args.kwargs
	assert kwargs["max_per_hour"] == DEFAULT_MAX_TASKS_PER_HOUR
