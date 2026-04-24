"""Unit tests for alfred.middleware.rate_limit.check_rate_limit.

The existing TestRateLimit / TestWebSocketRateLimit classes in
test_api_gateway.py exercise the REST + WebSocket paths end-to-end but
only run when a real Redis is available on 127.0.0.1:11000 (CI). These
tests use an AsyncMock Redis so the rate-limit logic itself and the
Prometheus counter wiring (TD-C6) are verified on every run.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.middleware.rate_limit import check_rate_limit


def _make_redis_mock(current_count: int, oldest_score: float = 0.0):
	"""Build an AsyncMock Redis whose pipeline.execute returns a zcard of
	``current_count`` (post-add). zrange for retry_after returns a single
	oldest entry with the given score.
	"""
	redis = AsyncMock()
	pipe = MagicMock()
	# Pipeline method chain: zremrangebyscore, zcard, zadd, expire — each
	# returns the pipe for chaining; execute returns the list of results.
	for m in ("zremrangebyscore", "zcard", "zadd", "expire"):
		setattr(pipe, m, MagicMock(return_value=pipe))
	pipe.execute = AsyncMock(return_value=[0, current_count, 1, True])
	redis.pipeline = MagicMock(return_value=pipe)
	redis.zrange = AsyncMock(return_value=[(f"{oldest_score}", oldest_score)])
	redis.zrem = AsyncMock(return_value=1)
	return redis


# ── No-redis / unlimited paths ─────────────────────────────────────


async def test_none_redis_allows_all():
	allowed, remaining, retry = await check_rate_limit(None, "site", "u", max_per_hour=10)
	assert allowed is True
	assert remaining == 10
	assert retry == 0


async def test_zero_max_means_unlimited():
	redis = _make_redis_mock(current_count=999)
	allowed, remaining, retry = await check_rate_limit(redis, "site", "u", max_per_hour=0)
	assert allowed is True
	assert remaining == -1
	assert retry == 0


# ── Allowed / blocked branches ─────────────────────────────────────


async def test_under_limit_allowed():
	redis = _make_redis_mock(current_count=2)  # 2 prior + this one = 3
	allowed, remaining, retry = await check_rate_limit(
		redis, "site", "u", max_per_hour=10, source="websocket",
	)
	assert allowed is True
	assert remaining == 7  # 10 - 2 - 1
	assert retry == 0


async def test_at_limit_blocked():
	# current_count reflects entries INCLUDING the one just added.
	# If max is 5 and current_count is 5, the just-added entry pushed
	# us to the threshold - block path.
	import time
	redis = _make_redis_mock(current_count=5, oldest_score=time.time() - 100)
	allowed, remaining, retry = await check_rate_limit(
		redis, "site", "u", max_per_hour=5, source="websocket",
	)
	assert allowed is False
	assert remaining == 0
	assert retry > 0
	# Rolled back the just-added entry.
	redis.zrem.assert_awaited_once()


# ── Prometheus counter increments on block ─────────────────────────


async def test_counter_increments_on_block_with_source_label():
	from alfred.obs.metrics import rate_limit_block_total

	# Snapshot the current count for the "websocket" source so we can
	# assert a delta of exactly 1 (the metric is global, other tests may
	# have touched it).
	def _val():
		try:
			return rate_limit_block_total.labels(source="websocket")._value.get()
		except (KeyError, AttributeError):
			return 0

	before = _val()

	import time
	redis = _make_redis_mock(current_count=1, oldest_score=time.time() - 100)
	allowed, _, _ = await check_rate_limit(
		redis, "site", "u", max_per_hour=1, source="websocket",
	)
	assert allowed is False

	after = _val()
	assert after == before + 1


async def test_counter_does_not_increment_on_allow():
	from alfred.obs.metrics import rate_limit_block_total

	def _val():
		try:
			return rate_limit_block_total.labels(source="websocket")._value.get()
		except (KeyError, AttributeError):
			return 0

	before = _val()

	redis = _make_redis_mock(current_count=1)
	allowed, _, _ = await check_rate_limit(
		redis, "site", "u", max_per_hour=10, source="websocket",
	)
	assert allowed is True

	after = _val()
	assert after == before


async def test_counter_distinguishes_rest_vs_websocket_source():
	from alfred.obs.metrics import rate_limit_block_total

	def _val(source):
		try:
			return rate_limit_block_total.labels(source=source)._value.get()
		except (KeyError, AttributeError):
			return 0

	rest_before = _val("rest")
	ws_before = _val("websocket")

	import time
	redis = _make_redis_mock(current_count=1, oldest_score=time.time() - 100)

	# One REST block, one WebSocket block.
	await check_rate_limit(redis, "site", "u1", max_per_hour=1, source="rest")
	await check_rate_limit(redis, "site", "u2", max_per_hour=1, source="websocket")

	assert _val("rest") == rest_before + 1
	assert _val("websocket") == ws_before + 1
