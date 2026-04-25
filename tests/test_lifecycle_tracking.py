"""Tests for the TD-M6 graceful-shutdown wire.

The lifespan handler in ``alfred/main.py`` polls
``app.state.active_pipelines`` to decide when it can finish shutting
down. Before this wire landed, that counter was initialised but never
moved by anything — every deploy hard-killed in-flight work.

These tests pin three contracts:
  - ``track_pipeline`` actually moves the counter, including under
    cancellation / exception paths;
  - the WS prompt handler refuses new prompts with ``SHUTTING_DOWN``
    once ``app.state.shutting_down`` is True;
  - ``POST /api/v1/tasks`` returns 503 + ``SHUTTING_DOWN`` in the
    same state, so REST callers retry with the right backoff.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.api.lifecycle import is_shutting_down, track_pipeline


class TestTrackPipelineCounter:
	@pytest.mark.asyncio
	async def test_increments_during_run_and_releases_on_clean_exit(self):
		"""Healthy path: enter +1, exit -1, balance is zero."""
		app_state = SimpleNamespace(active_pipelines=0)

		async with track_pipeline(app_state):
			assert app_state.active_pipelines == 1
		assert app_state.active_pipelines == 0

	@pytest.mark.asyncio
	async def test_releases_on_exception(self):
		"""Crash path: counter must NOT leak — that would block shutdown
		forever after a single bad run."""
		app_state = SimpleNamespace(active_pipelines=0)

		with pytest.raises(RuntimeError):
			async with track_pipeline(app_state):
				assert app_state.active_pipelines == 1
				raise RuntimeError("crew exploded")
		assert app_state.active_pipelines == 0

	@pytest.mark.asyncio
	async def test_releases_on_cancellation(self):
		"""Cancellation path: WS disconnect cancels the pipeline task.
		The finally block must still run so the counter doesn't leak."""
		app_state = SimpleNamespace(active_pipelines=0)

		async def _runner():
			async with track_pipeline(app_state):
				await asyncio.sleep(10)  # never completes

		task = asyncio.create_task(_runner())
		# Yield control once so the runner enters the context.
		await asyncio.sleep(0)
		assert app_state.active_pipelines == 1

		task.cancel()
		try:
			await task
		except asyncio.CancelledError:
			pass
		assert app_state.active_pipelines == 0

	@pytest.mark.asyncio
	async def test_handles_concurrent_pipelines(self):
		"""N concurrent runs → counter peaks at N, settles at 0."""
		app_state = SimpleNamespace(active_pipelines=0)
		mid = asyncio.Event()
		release = asyncio.Event()

		async def _runner():
			async with track_pipeline(app_state):
				mid.set()
				await release.wait()

		tasks = [asyncio.create_task(_runner()) for _ in range(5)]
		# Wait until at least one is inside the context …
		await mid.wait()
		# … then poll briefly so all five are in. Counter should be 5
		# without explicit synchronisation because asyncio runs them
		# cooperatively on one loop.
		for _ in range(20):
			if app_state.active_pipelines == 5:
				break
			await asyncio.sleep(0.01)
		assert app_state.active_pipelines == 5

		release.set()
		await asyncio.gather(*tasks)
		assert app_state.active_pipelines == 0

	@pytest.mark.asyncio
	async def test_counter_clamped_at_zero(self):
		"""Defends against a future double-decrement bug — the shutdown
		poll loop checks ``> 0`` and a negative value would silently
		exit early."""
		app_state = SimpleNamespace(active_pipelines=0)
		# Force a stale starting value as if a wire bug already
		# decremented once too many.
		app_state.active_pipelines = -5

		async with track_pipeline(app_state):
			assert app_state.active_pipelines == -4
		# After the finally, max(0, -5) = 0 — never sub-zero.
		assert app_state.active_pipelines == 0

	def test_is_shutting_down_defaults_to_false(self):
		"""Test path that never invokes the lifespan handler must not
		short-circuit every pipeline call into a 503."""
		app_state = SimpleNamespace()
		assert is_shutting_down(app_state) is False

	def test_is_shutting_down_reflects_attribute(self):
		assert is_shutting_down(SimpleNamespace(shutting_down=True)) is True
		assert is_shutting_down(SimpleNamespace(shutting_down=False)) is False


class TestWebSocketShutdownGate:
	"""WS ``prompt`` frame must be rejected with SHUTTING_DOWN once the
	lifespan flips."""

	@pytest.mark.asyncio
	async def test_prompt_rejected_when_shutting_down(self):
		from alfred.api.websocket.connection import (
			ConnectionState,
			_handle_custom_message,
		)

		ws = MagicMock()
		ws.send_json = AsyncMock()
		ws.app = MagicMock()
		ws.app.state = SimpleNamespace(shutting_down=True, active_pipelines=0)

		conn = ConnectionState(
			websocket=ws, site_id="site-a", user="alice",
			roles=[], site_config={}, conversation_id="conv-shut",
		)

		await _handle_custom_message(
			data={"type": "prompt", "msg_id": "m1", "data": {"text": "hi"}},
			websocket=ws, conn=conn, conversation_id="conv-shut",
		)

		# Expect exactly one error frame with the new code.
		ws.send_json.assert_awaited_once()
		sent = ws.send_json.await_args.args[0]
		assert sent["type"] == "error"
		assert sent["data"]["code"] == "SHUTTING_DOWN"
		# Pipeline must NOT have been spawned.
		assert conn.active_pipeline is None


class TestRestShutdownGate:
	"""``POST /api/v1/tasks`` must return 503 + SHUTTING_DOWN once the
	lifespan flips. The retry-after backoff is implicit (clients SHOULD
	retry within a few seconds for the new instance to be up)."""

	@pytest.mark.asyncio
	async def test_post_returns_503_when_shutting_down(self, monkeypatch):
		import os
		# API_SECRET_KEY validator floor in alfred.config — keep above
		# the 32-byte minimum so Settings() doesn't reject the test.
		monkeypatch.setenv(
			"API_SECRET_KEY",
			"test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4",
		)
		# Provide an explicit origin so the production-strict CORS check in
		# create_app() doesn't trip on the default ``*``.
		monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8001")
		os.environ.pop("REDIS_URL", None)

		from alfred.config import get_settings
		get_settings.cache_clear()

		from alfred.main import create_app
		app = create_app()
		app.state.settings = get_settings()
		# Force the gate state without driving the full lifespan.
		app.state.shutting_down = True
		app.state.active_pipelines = 0
		# Provide a fake redis so the route doesn't bail at 503/REDIS_UNAVAILABLE
		# before our shutdown gate can fire.
		app.state.redis = MagicMock()

		from httpx import ASGITransport, AsyncClient
		transport = ASGITransport(app=app)
		async with AsyncClient(transport=transport, base_url="http://test") as ac:
			resp = await ac.post(
				"/api/v1/tasks",
				headers={
					"Authorization": "Bearer test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4",
				},
				json={
					"prompt": "x",
					"site_config": {"site_id": "site-a"},
					"user_context": {"user": "alice"},
				},
			)

		assert resp.status_code == 503
		body = resp.json()
		# detail can be string or dict depending on FastAPI version handling;
		# the route uses dict-shape so unwrap safely.
		detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
		# Some FastAPI versions JSON-string the detail; tolerate both shapes.
		if isinstance(detail, str):
			detail = json.loads(detail)
		assert detail["code"] == "SHUTTING_DOWN"

		get_settings.cache_clear()
