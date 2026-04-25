"""Regression test for the audit's P1.3 — REST concurrent-task cap.

The HTTP rate limiter caps requests-per-hour but not concurrent runs.
A burst POST loop fired all 20/hour requests within the window and
stacked 20 concurrent ``AgentPipeline`` runs on the LLM thread pool,
denying service to other tenants.

The fix: a per-(site_id, user) in-process counter
(``alfred.api.rest_runner._concurrent_tasks``) increments on
``schedule_rest_task`` and decrements in the runner's ``finally``.
``schedule_rest_task`` returns ``False`` when the cap is hit;
``POST /api/v1/tasks`` translates that to HTTP 429 +
``CONCURRENT_LIMIT``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alfred.api import rest_runner
from alfred.api.rest_runner import (
	_concurrent_tasks,
	concurrent_count,
	schedule_rest_task,
)
from alfred.models.messages import TaskCreateRequest


@pytest.fixture(autouse=True)
def _reset_counter():
	"""Per-user counter is module-level state — clear it between
	tests so leftovers from one test don't trip another's cap check."""
	_concurrent_tasks.clear()
	yield
	_concurrent_tasks.clear()


def _settings(cap: int = 2) -> SimpleNamespace:
	return SimpleNamespace(MAX_CONCURRENT_REST_TASKS_PER_USER=cap)


def _body(*, site: str = "site-a", user: str = "alice") -> TaskCreateRequest:
	return TaskCreateRequest(
		prompt="x",
		site_config={"site_id": site},
		user_context={"user": user},
	)


def test_schedule_returns_true_below_cap(monkeypatch):
	"""Up to ``cap`` calls schedule cleanly. Each one is reflected in
	the per-user counter."""
	# Stub spawn_logged so no real coroutine fires.
	monkeypatch.setattr(rest_runner, "spawn_logged", lambda coro, name: coro.close() or MagicMock())

	settings = _settings(cap=2)
	for i in range(2):
		ok = schedule_rest_task(
			task_id=f"t-{i}", body=_body(),
			redis=MagicMock(), settings=settings, store=MagicMock(),
		)
		assert ok is True
	assert concurrent_count("site-a", "alice") == 2


def test_schedule_returns_false_at_cap(monkeypatch):
	"""Third call when cap=2 must return False without spawning."""
	spawn_calls = []
	def _capture(coro, name):
		spawn_calls.append(name)
		coro.close()
		return MagicMock()
	monkeypatch.setattr(rest_runner, "spawn_logged", _capture)

	settings = _settings(cap=2)
	# Fill the cap.
	schedule_rest_task(task_id="t-1", body=_body(), redis=MagicMock(),
		settings=settings, store=MagicMock())
	schedule_rest_task(task_id="t-2", body=_body(), redis=MagicMock(),
		settings=settings, store=MagicMock())
	assert len(spawn_calls) == 2

	# Third one — rejected.
	ok = schedule_rest_task(task_id="t-3", body=_body(), redis=MagicMock(),
		settings=settings, store=MagicMock())
	assert ok is False
	# spawn_logged not called for the rejected task.
	assert len(spawn_calls) == 2
	# Counter unchanged from the cap-fill above.
	assert concurrent_count("site-a", "alice") == 2


def test_per_user_cap_is_independent(monkeypatch):
	"""User A hitting their cap doesn't block User B from scheduling."""
	monkeypatch.setattr(rest_runner, "spawn_logged", lambda coro, name: coro.close() or MagicMock())
	settings = _settings(cap=1)

	# Alice fills her quota.
	assert schedule_rest_task(task_id="ta", body=_body(user="alice"),
		redis=MagicMock(), settings=settings, store=MagicMock()) is True
	# Alice's second is rejected.
	assert schedule_rest_task(task_id="ta2", body=_body(user="alice"),
		redis=MagicMock(), settings=settings, store=MagicMock()) is False
	# Bob is independent and goes through.
	assert schedule_rest_task(task_id="tb", body=_body(user="bob"),
		redis=MagicMock(), settings=settings, store=MagicMock()) is True

	assert concurrent_count("site-a", "alice") == 1
	assert concurrent_count("site-a", "bob") == 1


def test_cap_zero_disables_check(monkeypatch):
	"""A cap of 0 (or negative) is treated as unlimited — operator
	can intentionally disable the cap for trusted environments."""
	monkeypatch.setattr(rest_runner, "spawn_logged", lambda coro, name: coro.close() or MagicMock())
	settings = _settings(cap=0)

	for i in range(50):
		assert schedule_rest_task(task_id=f"t-{i}", body=_body(),
			redis=MagicMock(), settings=settings, store=MagicMock()) is True
	assert concurrent_count("site-a", "alice") == 50


@pytest.mark.asyncio
async def test_runner_decrements_counter_on_completion(monkeypatch):
	"""End-to-end: scheduling increments, the runner's finally
	decrements. Two consecutive runs should each return the slot."""
	from alfred.api.rest_runner import _run_rest_task

	# In-memory store stub.
	class _S:
		def __init__(self):
			self.tasks: dict = {}
		async def get_task_state(self, s, t):
			return self.tasks.get((s, t))
		async def set_task_state(self, s, t, state, ttl_seconds=None):
			self.tasks[(s, t)] = dict(state)
		async def push_event(self, *_, **__):
			return "id"
		async def set_current_agent(self, *_, **__):
			return None

	store = _S()
	store.tasks[("site-a", "t-x")] = {"task_id": "t-x", "status": "queued"}

	# Stub pipeline so it finishes cleanly without calling LLMs.
	class _StubPipeline:
		def __init__(self, ctx):
			pass
		async def run(self):
			return None
	monkeypatch.setattr("alfred.api.pipeline.AgentPipeline", _StubPipeline)

	# Pre-increment as schedule_rest_task would have done.
	_concurrent_tasks[("site-a", "alice")] = 1
	assert concurrent_count("site-a", "alice") == 1

	await _run_rest_task(
		task_id="t-x", body=_body(),
		redis=MagicMock(), settings=_settings(cap=2), store=store,
	)

	# Counter back to zero — slot returned.
	assert concurrent_count("site-a", "alice") == 0
	# Key fully removed (no orphan zero entry — the cleanup avoids
	# unbounded dict growth across many distinct users).
	assert ("site-a", "alice") not in _concurrent_tasks


@pytest.mark.asyncio
async def test_runner_decrements_counter_even_on_pipeline_crash(monkeypatch):
	"""Pipeline raising must NOT leak the slot — that would block the
	user from ever submitting again until process restart."""
	from alfred.api.rest_runner import _run_rest_task

	class _S:
		def __init__(self):
			self.tasks = {}
		async def get_task_state(self, s, t):
			return self.tasks.get((s, t))
		async def set_task_state(self, s, t, state, ttl_seconds=None):
			self.tasks[(s, t)] = dict(state)
		async def push_event(self, *_, **__):
			return "id"
		async def set_current_agent(self, *_, **__):
			return None

	store = _S()
	store.tasks[("site-a", "t-crash")] = {"task_id": "t-crash", "status": "queued"}

	class _CrashPipeline:
		def __init__(self, ctx):
			pass
		async def run(self):
			raise RuntimeError("boom")
	monkeypatch.setattr("alfred.api.pipeline.AgentPipeline", _CrashPipeline)

	_concurrent_tasks[("site-a", "alice")] = 1
	await _run_rest_task(
		task_id="t-crash", body=_body(),
		redis=MagicMock(), settings=_settings(cap=2), store=store,
	)

	# Slot returned even though the pipeline raised.
	assert concurrent_count("site-a", "alice") == 0
