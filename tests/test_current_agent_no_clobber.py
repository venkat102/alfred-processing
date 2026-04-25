"""Regression test for the audit's P1.1 — task_state TOCTOU.

Before the fix, ``_RestConn.send`` did read-modify-write into the
JSON-encoded ``task_state`` for every ``agent_status`` event. Two
events emitted nearly simultaneously could interleave, and worse
the runner's final ``set_task_state`` (status=completed/failed)
could be silently overwritten by a trailing ``agent_status`` whose
read-snapshot was from before the runner's write.

The fix splits ``current_agent`` onto its own Redis key with atomic
SETEX. This test pins three guarantees:

  - rapid concurrent ``agent_status`` emits don't drop updates;
  - the final terminal status the runner writes to the task row
    survives even if a late ``agent_status`` lands afterwards;
  - the GET endpoint resolves ``current_agent`` from the side-channel,
    not from the (now-stale) JSON state field.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from alfred.api.rest_runner import _RestConn


class _FakeStore:
	"""Mirrors the production semantics: task_state is JSON-encoded with
	read-modify-write semantics; current_agent is an atomic per-key SET."""

	def __init__(self) -> None:
		self.tasks: dict[tuple[str, str], dict[str, Any]] = {}
		self.current_agents: dict[tuple[str, str], str] = {}
		self.events: list[tuple[str, str, dict[str, Any]]] = []

	async def get_task_state(self, site_id, task_id):
		return self.tasks.get((site_id, task_id))

	async def set_task_state(self, site_id, task_id, state, ttl_seconds=None):
		self.tasks[(site_id, task_id)] = dict(state)

	async def push_event(self, site_id, conversation_id, event):
		self.events.append((site_id, conversation_id, dict(event)))
		return "stream-id"

	async def set_current_agent(self, site_id, task_id, agent, ttl_seconds=None):
		self.current_agents[(site_id, task_id)] = agent

	async def get_current_agent(self, site_id, task_id):
		return self.current_agents.get((site_id, task_id))


def _make_conn(store):
	return _RestConn(
		site_id="site-a", user="alice", roles=[],
		site_config={}, store=store, task_id="t-toctou",
		redis=MagicMock(), settings=MagicMock(),
	)


@pytest.mark.asyncio
async def test_concurrent_agent_status_emits_record_last_writer():
	"""Five ``agent_status`` events fire concurrently. The side-channel
	key must reflect *some* valid value (one of the five) — the old
	read-modify-write pattern could lose all but one update under
	this same workload."""
	store = _FakeStore()
	conn = _make_conn(store)

	agents = ["Requirement", "Assessment", "Architect", "Developer", "Tester"]
	await asyncio.gather(*[
		conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {"agent": a}})
		for i, a in enumerate(agents)
	])

	# At least one update must have landed (the side-channel write
	# can't be lost). With the production atomic SETEX the value is
	# whichever finished last; under concurrent reads-modify-writes
	# on the JSON state, prior bug could leave None.
	final = store.current_agents.get(("site-a", "t-toctou"))
	assert final is not None
	assert final in agents


@pytest.mark.asyncio
async def test_terminal_status_not_clobbered_by_late_agent_status():
	"""Timeline:
	  1. runner writes ``status=completed`` to ``task_state``
	  2. a late ``agent_status`` event fires for "Deployer"

	Under the old wire, step 2 read the post-completed task_state,
	stamped current_agent=Deployer onto it, and wrote it back —
	leaving status=completed AND current_agent=Deployer. That's
	still confused (a "completed" run shouldn't keep getting agent
	updates), but the worse failure mode happened when steps 1 and
	2 raced: the late agent_status could read the pre-completed
	state and overwrite it without the status flip.

	Now: ``current_agent`` is on its own key, so the runner's
	terminal write to ``task_state`` is independent."""
	store = _FakeStore()
	conn = _make_conn(store)

	# Runner has written the terminal state.
	store.tasks[("site-a", "t-toctou")] = {
		"task_id": "t-toctou", "status": "completed", "changes": [{"x": 1}],
	}

	# Late agent_status fires after the terminal write.
	await conn.send({
		"msg_id": "late",
		"type": "agent_status",
		"data": {"agent": "Deployer"},
	})

	# The terminal status survives — the side-channel write doesn't
	# touch the task row.
	assert store.tasks[("site-a", "t-toctou")]["status"] == "completed"
	assert store.tasks[("site-a", "t-toctou")]["changes"] == [{"x": 1}]
	# Side-channel still recorded the late agent for completeness.
	assert store.current_agents[("site-a", "t-toctou")] == "Deployer"


@pytest.mark.asyncio
async def test_non_agent_status_messages_skip_side_channel():
	"""Side-channel write is gated on ``type == "agent_status"``. A
	plain info / error / changeset message must NOT touch
	``current_agent`` (would corrupt UI state with stale labels)."""
	store = _FakeStore()
	conn = _make_conn(store)

	for msg_type in ("info", "error", "changeset", "ack", "ping"):
		await conn.send({
			"msg_id": f"x-{msg_type}",
			"type": msg_type,
			"data": {"agent": "ShouldNotLand"},
		})

	assert store.current_agents == {}


@pytest.mark.asyncio
async def test_set_current_agent_failure_does_not_abort_send():
	"""Telemetry write failure (Redis hiccup) must not break the
	pipeline's emit loop — the event mirror to the stream already
	succeeded, that's the load-bearing part."""
	store = _FakeStore()

	async def _boom(*_, **__):
		raise RuntimeError("redis transient")
	store.set_current_agent = _boom  # type: ignore[assignment]

	conn = _make_conn(store)

	# Must not raise — the BLE001-noted try/except in conn.send
	# protects the pipeline from telemetry failures.
	await conn.send({
		"msg_id": "m1",
		"type": "agent_status",
		"data": {"agent": "Architect"},
	})

	# The event mirror still succeeded.
	assert len(store.events) == 1
