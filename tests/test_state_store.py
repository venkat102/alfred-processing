"""Tests for the Redis state store.

Requires a running Redis instance. Uses the bench Redis on port 11000
or falls back to default port 6379.
"""

import asyncio
import json

import pytest
import redis.asyncio as aioredis

from alfred.state.store import StateStore

REDIS_URL = "redis://127.0.0.1:11000/1"  # Use DB 1 for test isolation


@pytest.fixture
async def redis_client():
	"""Create a Redis client for tests, flush test DB on teardown."""
	client = aioredis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
	try:
		await client.ping()
	except (aioredis.RedisError, OSError):
		pytest.skip("Redis not available - skipping state store tests")
	yield client
	# Cleanup: delete all alfred:* keys in test DB
	keys = []
	async for key in client.scan_iter("alfred:*"):
		keys.append(key)
	if keys:
		await client.delete(*keys)
	await client.aclose()


@pytest.fixture
def store(redis_client):
	return StateStore(redis_client)


# ── Task State CRUD ──────────────────────────────────────────────


class TestTaskState:
	async def test_set_and_get(self, store):
		state = {"status": "running", "agent": "requirement", "step": 1}
		await store.set_task_state("site1", "task-abc", state)
		result = await store.get_task_state("site1", "task-abc")
		assert result == state

	async def test_get_nonexistent(self, store):
		result = await store.get_task_state("site1", "nonexistent-task")
		assert result is None

	async def test_update_state(self, store):
		await store.set_task_state("site1", "task-1", {"status": "running"})
		await store.set_task_state(
			"site1", "task-1", {"status": "completed", "result": "ok"}
		)
		result = await store.get_task_state("site1", "task-1")
		assert result["status"] == "completed"
		assert result["result"] == "ok"

	async def test_delete(self, store):
		await store.set_task_state("site1", "task-del", {"status": "done"})
		deleted = await store.delete_task_state("site1", "task-del")
		assert deleted is True
		result = await store.get_task_state("site1", "task-del")
		assert result is None

	async def test_delete_nonexistent(self, store):
		deleted = await store.delete_task_state("site1", "no-such-task")
		assert deleted is False

	async def test_invalid_site_id_empty(self, store):
		with pytest.raises(ValueError, match="site_id cannot be empty"):
			await store.set_task_state("", "task-1", {"x": 1})

	async def test_invalid_site_id_special_chars(self, store):
		with pytest.raises(ValueError, match="contains invalid characters"):
			await store.set_task_state("site:evil", "task-1", {"x": 1})

	async def test_non_serializable_state(self, store):
		with pytest.raises(TypeError, match="not JSON-serializable"):
			await store.set_task_state("site1", "task-1", {"func": lambda x: x})


# ── Namespace Isolation ──────────────────────────────────────────


class TestNamespaceIsolation:
	async def test_sites_are_isolated(self, store):
		await store.set_task_state("site-a", "task-1", {"owner": "A"})
		await store.set_task_state("site-b", "task-1", {"owner": "B"})

		result_a = await store.get_task_state("site-a", "task-1")
		result_b = await store.get_task_state("site-b", "task-1")

		assert result_a["owner"] == "A"
		assert result_b["owner"] == "B"

	async def test_delete_doesnt_cross_sites(self, store):
		await store.set_task_state("site-a", "task-x", {"data": "a"})
		await store.set_task_state("site-b", "task-x", {"data": "b"})

		await store.delete_task_state("site-a", "task-x")

		assert await store.get_task_state("site-a", "task-x") is None
		assert await store.get_task_state("site-b", "task-x") is not None


# ── Event Stream ─────────────────────────────────────────────────


class TestEventStream:
	async def test_push_and_read_events(self, store):
		events = [
			{"type": "agent_started", "agent": "requirement"},
			{"type": "question", "text": "What fields?"},
			{"type": "agent_finished", "agent": "requirement"},
		]
		for event in events:
			await store.push_event("site1", "conv-123", event)

		result = await store.get_events("site1", "conv-123", since_id="0")
		assert len(result) == 3
		assert result[0]["data"]["type"] == "agent_started"
		assert result[1]["data"]["type"] == "question"
		assert result[2]["data"]["type"] == "agent_finished"

	async def test_events_since_id(self, store):
		ids = []
		for i in range(5):
			entry_id = await store.push_event("site1", "conv-since", {"seq": i})
			ids.append(entry_id)

		# Read events after the 2nd one
		result = await store.get_events("site1", "conv-since", since_id=ids[1])
		assert len(result) == 3  # events 2, 3, 4
		assert result[0]["data"]["seq"] == 2

	async def test_events_empty_stream(self, store):
		result = await store.get_events("site1", "conv-empty", since_id="0")
		assert result == []

	async def test_events_isolated_by_site(self, store):
		await store.push_event("site-a", "conv-1", {"site": "A"})
		await store.push_event("site-b", "conv-1", {"site": "B"})

		events_a = await store.get_events("site-a", "conv-1", since_id="0")
		events_b = await store.get_events("site-b", "conv-1", since_id="0")

		assert len(events_a) == 1
		assert events_a[0]["data"]["site"] == "A"
		assert len(events_b) == 1
		assert events_b[0]["data"]["site"] == "B"

	async def test_push_non_serializable_event(self, store):
		with pytest.raises(TypeError, match="not JSON-serializable"):
			await store.push_event("site1", "conv-1", {"bad": set()})


# ── TTL Cache ────────────────────────────────────────────────────


class TestTTLCache:
	async def test_set_and_get_cached(self, store):
		await store.set_with_ttl(
			"site1", "plan-info", '{"tier": "pro"}', ttl_seconds=60
		)
		result = await store.get_cached("site1", "plan-info")
		assert result == '{"tier": "pro"}'

	async def test_ttl_expiration(self, store):
		await store.set_with_ttl("site1", "ephemeral", "value", ttl_seconds=1)
		result = await store.get_cached("site1", "ephemeral")
		assert result == "value"

		await asyncio.sleep(1.5)
		result = await store.get_cached("site1", "ephemeral")
		assert result is None

	async def test_invalid_ttl(self, store):
		with pytest.raises(ValueError, match="ttl_seconds must be positive"):
			await store.set_with_ttl("site1", "key", "val", ttl_seconds=0)

		with pytest.raises(ValueError, match="ttl_seconds must be positive"):
			await store.set_with_ttl("site1", "key", "val", ttl_seconds=-5)


# ── Health Check ─────────────────────────────────────────────────


class TestHealthCheck:
	async def test_healthy(self, store):
		assert await store.is_healthy() is True
