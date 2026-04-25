"""Unit tests for the REST-driven pipeline runner.

The runner is the missing piece that closes the audit's C2: before this
landed, ``POST /api/v1/tasks`` wrote ``status="queued"`` to Redis and
nothing on the planet drained that queue. The tests here pin the
contract that the runner now:

  - moves the task through ``queued -> running -> completed`` on a
    healthy run;
  - moves it to ``failed`` when the pipeline raises;
  - mirrors emitted messages into the Redis event stream so the
    companion ``GET /api/v1/tasks/{id}/messages`` poll endpoint sees
    intermediate progress;
  - still releases the ``contextvars`` frame even on crash, so a
    later run on the same loop doesn't inherit stale identity.

The pipeline itself is stubbed — these are not integration tests for
the agent crew, they're contract tests for the runner glue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from alfred.api.rest_runner import _RestConn, _run_rest_task
from alfred.models.messages import TaskCreateRequest


class _FakeStore:
	"""Minimal in-memory stand-in for ``StateStore`` covering only the
	methods the runner touches. Keeps tests independent of Redis."""

	def __init__(self) -> None:
		self.tasks: dict[tuple[str, str], dict[str, Any]] = {}
		self.events: list[tuple[str, str, dict[str, Any]]] = []
		# P1.1 side-channel: current_agent is its own atomic Redis key
		# in production, modeled here as a flat dict keyed identically
		# to the task row.
		self.current_agents: dict[tuple[str, str], str] = {}

	async def get_task_state(self, site_id: str, task_id: str):
		return self.tasks.get((site_id, task_id))

	async def set_task_state(self, site_id: str, task_id: str, state, ttl_seconds=None):
		# Copy so later mutations of the returned ref don't bleed back.
		self.tasks[(site_id, task_id)] = dict(state)

	async def push_event(self, site_id: str, conversation_id: str, event):
		self.events.append((site_id, conversation_id, dict(event)))
		return f"stream-{len(self.events)}"

	async def set_current_agent(self, site_id: str, task_id: str, agent, ttl_seconds=None):
		self.current_agents[(site_id, task_id)] = agent

	async def get_current_agent(self, site_id: str, task_id: str):
		return self.current_agents.get((site_id, task_id))


def _body(prompt: str = "Create a Customer doctype") -> TaskCreateRequest:
	return TaskCreateRequest(
		prompt=prompt,
		site_config={"site_id": "site-a"},
		user_context={"user": "alice@example.com", "roles": ["System Manager"]},
	)


@pytest.mark.asyncio
async def test_runner_marks_running_then_completed_on_clean_run():
	"""Healthy pipeline path: status walks queued -> running -> completed."""
	store = _FakeStore()
	store.tasks[("site-a", "t1")] = {"task_id": "t1", "status": "queued"}

	# Stub AgentPipeline so the test doesn't need Ollama / a crew. The
	# stub is a benign no-op .run() — pipeline finishes without setting
	# should_stop, so the runner classifies the run as completed.
	class _StubPipeline:
		def __init__(self, ctx):
			self.ctx = ctx

		async def run(self):
			# Set a tiny bit of state so the final write is realistic.
			self.ctx.changes = [{"op": "create", "doctype": "Customer"}]
			self.ctx.mode = "dev"

	with patch("alfred.api.pipeline.AgentPipeline", _StubPipeline):
		await _run_rest_task(
			task_id="t1", body=_body(),
			redis=MagicMock(), settings=MagicMock(), store=store,
		)

	final = store.tasks[("site-a", "t1")]
	assert final["status"] == "completed"
	assert final["mode"] == "dev"
	assert final["changes"] == [{"op": "create", "doctype": "Customer"}]


@pytest.mark.asyncio
async def test_runner_marks_failed_when_pipeline_stops():
	"""Pipeline calls ``ctx.stop()`` (e.g. OLLAMA_UNHEALTHY at warmup);
	the runner must record status=failed plus the error message so a
	polling client knows the run won't progress further."""
	store = _FakeStore()
	store.tasks[("site-a", "t2")] = {"task_id": "t2", "status": "queued"}

	class _StoppingPipeline:
		def __init__(self, ctx):
			self.ctx = ctx

		async def run(self):
			self.ctx.stop("Ollama unreachable", code="OLLAMA_UNHEALTHY")

	with patch("alfred.api.pipeline.AgentPipeline", _StoppingPipeline):
		await _run_rest_task(
			task_id="t2", body=_body(),
			redis=MagicMock(), settings=MagicMock(), store=store,
		)

	final = store.tasks[("site-a", "t2")]
	assert final["status"] == "failed"
	assert final["error"] == "Ollama unreachable"


@pytest.mark.asyncio
async def test_runner_marks_failed_when_pipeline_raises():
	"""An uncaught exception inside ``AgentPipeline.run`` must surface
	as status=failed with the exception text — never a silent task
	stuck on ``running`` forever."""
	store = _FakeStore()
	store.tasks[("site-a", "t3")] = {"task_id": "t3", "status": "queued"}

	class _CrashingPipeline:
		def __init__(self, ctx):
			pass

		async def run(self):
			raise RuntimeError("crew exploded")

	with patch("alfred.api.pipeline.AgentPipeline", _CrashingPipeline):
		await _run_rest_task(
			task_id="t3", body=_body(),
			redis=MagicMock(), settings=MagicMock(), store=store,
		)

	final = store.tasks[("site-a", "t3")]
	assert final["status"] == "failed"
	assert "crew exploded" in final["error"]


@pytest.mark.asyncio
async def test_runner_skips_when_task_state_missing():
	"""TTL eviction between POST and the spawn. Don't fabricate a row
	we never owned — just log + return."""
	store = _FakeStore()  # empty: no row for ("site-a", "t-missing")

	called = MagicMock()

	class _ShouldNotRunPipeline:
		def __init__(self, ctx):
			called.constructed()

		async def run(self):
			called.ran()

	with patch("alfred.api.pipeline.AgentPipeline", _ShouldNotRunPipeline):
		await _run_rest_task(
			task_id="t-missing", body=_body(),
			redis=MagicMock(), settings=MagicMock(), store=store,
		)

	assert ("site-a", "t-missing") not in store.tasks
	called.constructed.assert_not_called()
	called.ran.assert_not_called()


@pytest.mark.asyncio
async def test_rest_conn_send_pushes_to_event_stream():
	"""The pipeline emits messages via ``conn.send``. The REST shim
	mirrors them into the stream so ``GET /tasks/{id}/messages`` shows
	the same trail a WebSocket client would."""
	store = _FakeStore()
	conn = _RestConn(
		site_id="site-a", user="alice", roles=[],
		site_config={}, store=store, task_id="t4",
		redis=MagicMock(), settings=MagicMock(),
	)

	await conn.send({"msg_id": "m1", "type": "agent_status", "data": {"agent": "Architect"}})

	# Stream picked up the event …
	assert len(store.events) == 1
	site_id, conv_id, ev = store.events[0]
	assert site_id == "site-a"
	# Conversation id mirrors task_id so GET /messages?site_id=...&since_id=
	# replays consistently.
	assert conv_id == "t4"
	assert ev["data"]["agent"] == "Architect"

	# … and ``current_agent`` updates on its own Redis key (the P1.1
	# fix split this off the task row so high-frequency agent_status
	# emits don't race with the runner's terminal status write).
	assert store.current_agents[("site-a", "t4")] == "Architect"


@pytest.mark.asyncio
async def test_rest_conn_exposes_app_state_for_pipeline_phases():
	"""``_phases_setup`` reads ``ctx.conn.websocket.app.state.{redis,settings}``.
	The shim must satisfy that read without faking a full WebSocket —
	a SimpleNamespace duck-type is enough."""
	redis = SimpleNamespace(name="redis-marker")
	settings = SimpleNamespace(name="settings-marker")
	conn = _RestConn(
		site_id="site-a", user="alice", roles=[],
		site_config={}, store=_FakeStore(), task_id="t5",
		redis=redis, settings=settings,
	)

	assert conn.websocket.app.state.redis is redis
	assert conn.websocket.app.state.settings is settings
	# REST has no MCP back-channel; pipeline guards (`if conn.mcp_client`)
	# rely on this being None to skip Frappe-introspection paths.
	assert conn.mcp_client is None


@pytest.mark.asyncio
async def test_runner_clears_request_context_even_on_crash():
	"""Regression guard: the structlog contextvars frame must be
	released in the runner's ``finally`` block. If we leak it, the
	next coroutine on this loop inherits stale ``site_id`` / ``user``
	fields."""
	store = _FakeStore()
	store.tasks[("site-a", "t6")] = {"task_id": "t6", "status": "queued"}

	class _CrashingPipeline:
		def __init__(self, ctx):
			pass

		async def run(self):
			raise RuntimeError("boom")

	with patch("alfred.api.pipeline.AgentPipeline", _CrashingPipeline), \
			patch("alfred.api.rest_runner.bind_request_context") as bind, \
			patch("alfred.api.rest_runner.clear_request_context") as clear:
		await _run_rest_task(
			task_id="t6", body=_body(),
			redis=MagicMock(), settings=MagicMock(), store=store,
		)

	bind.assert_called_once()
	clear.assert_called_once()
