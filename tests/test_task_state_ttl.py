"""Unit tests for TaskStateStore.set_task_state TTL wiring (TD-H5).

Uses AsyncMock Redis so these run portably, independent of whether a
real Redis is reachable. The existing tests/test_state_store.py
exercises the real Redis path and still passes (setex's read behaviour
is identical to set for our purposes).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.state.store import StateStore


def _store_with_mock():
	redis = MagicMock()
	redis.setex = AsyncMock(return_value=True)
	redis.set = AsyncMock(return_value=True)
	redis.get = AsyncMock(return_value=None)
	return StateStore(redis), redis


async def test_default_ttl_applied_from_settings():
	# Default TTL comes from Settings.TASK_STATE_TTL_SECONDS (7 days).
	store, redis = _store_with_mock()
	await store.set_task_state("s1", "t1", {"status": "ok"})
	redis.setex.assert_awaited_once()
	redis.set.assert_not_awaited()   # MUST not use the no-TTL set()
	args = redis.setex.call_args
	# setex(key, ttl, value)
	key, ttl, value = args.args
	assert key == "alfred:s1:task:t1"
	assert ttl == 604800   # 7 days
	assert '"status":' in value


async def test_explicit_ttl_override():
	store, redis = _store_with_mock()
	await store.set_task_state("s1", "t1", {"x": 1}, ttl_seconds=300)
	args = redis.setex.call_args
	_, ttl, _ = args.args
	assert ttl == 300


async def test_zero_ttl_rejected():
	store, redis = _store_with_mock()
	with pytest.raises(ValueError, match="ttl_seconds must be positive"):
		await store.set_task_state("s1", "t1", {"x": 1}, ttl_seconds=0)
	redis.setex.assert_not_awaited()


async def test_negative_ttl_rejected():
	store, redis = _store_with_mock()
	with pytest.raises(ValueError, match="ttl_seconds must be positive"):
		await store.set_task_state("s1", "t1", {"x": 1}, ttl_seconds=-60)


async def test_non_json_serializable_still_raises_typeerror():
	# TTL addition must not change the TypeError contract.
	store, redis = _store_with_mock()

	class NotSerializable:
		pass

	with pytest.raises(TypeError, match="not JSON-serializable"):
		await store.set_task_state("s1", "t1", {"obj": NotSerializable()})
	redis.setex.assert_not_awaited()


async def test_key_namespaced_by_site_id():
	store, redis = _store_with_mock()
	await store.set_task_state("site-xyz", "t42", {"ok": True})
	args = redis.setex.call_args
	key = args.args[0]
	assert key == "alfred:site-xyz:task:t42"


async def test_invalid_task_id_rejected():
	# Pre-existing validation must still run.
	store, redis = _store_with_mock()
	with pytest.raises(ValueError):
		await store.set_task_state("s1", "has spaces!", {"x": 1})
	redis.setex.assert_not_awaited()


async def test_large_ttl_accepted():
	# No upper bound on TTL — some workflows legitimately run multi-week.
	store, redis = _store_with_mock()
	one_year = 365 * 24 * 3600
	await store.set_task_state("s1", "t1", {"x": 1}, ttl_seconds=one_year)
	_, ttl, _ = redis.setex.call_args.args
	assert ttl == one_year
