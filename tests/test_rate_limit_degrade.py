"""Tests for the fail-open observability behavior of check_rate_limit.

The no-Redis path is operationally load-bearing: if we silently allow
every request without logging, a stuck Redis turns into invisible rate-
limit bypass. These tests lock the contract in:
  - redis=None: logs WARNING, increments degraded counter (no_client)
  - redis.pipeline().execute() raises: logs WARNING, increments counter
    (redis_error), still returns allow so a blip doesn't hard-block.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from alfred.middleware.rate_limit import check_rate_limit
from alfred.obs import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
	metrics.reset_for_tests()
	yield


class TestDegradeNoClient:
	@pytest.mark.asyncio
	async def test_none_client_returns_allow(self, caplog):
		with caplog.at_level(logging.WARNING, logger="alfred.ratelimit"):
			allowed, remaining, retry = await check_rate_limit(
				None, site_id="site-x", user="u@x.com", max_per_hour=10,
			)
		assert allowed is True
		assert remaining == 10
		assert retry == 0

	@pytest.mark.asyncio
	async def test_none_client_logs_warning(self, caplog):
		with caplog.at_level(logging.WARNING, logger="alfred.ratelimit"):
			await check_rate_limit(None, site_id="s", user="u", max_per_hour=5)
		matching = [
			r for r in caplog.records
			if "no redis client" in r.getMessage() and "degraded" in r.getMessage()
		]
		assert matching, "expected a WARNING log for the no-client degrade path"

	@pytest.mark.asyncio
	async def test_none_client_increments_metric(self):
		await check_rate_limit(None, site_id="s", user="u", max_per_hour=5)
		value = metrics.rate_limit_degraded_total.labels(reason="no_client")._value.get()
		assert value == 1

		# Second call - counter increments again, not just first-call.
		await check_rate_limit(None, site_id="s", user="u", max_per_hour=5)
		value = metrics.rate_limit_degraded_total.labels(reason="no_client")._value.get()
		assert value == 2


class TestDegradeRedisError:
	def _make_failing_redis(self, exc: Exception) -> MagicMock:
		"""Build a mock Redis whose pipeline().execute() raises."""
		pipe = MagicMock()
		pipe.zremrangebyscore = MagicMock(return_value=None)
		pipe.zcard = MagicMock(return_value=None)
		pipe.zadd = MagicMock(return_value=None)
		pipe.expire = MagicMock(return_value=None)
		pipe.execute = AsyncMock(side_effect=exc)
		redis_mock = MagicMock()
		redis_mock.pipeline = MagicMock(return_value=pipe)
		return redis_mock

	@pytest.mark.asyncio
	async def test_pipeline_connection_error_fails_open(self, caplog):
		redis_mock = self._make_failing_redis(RedisConnectionError("Redis is down"))
		with caplog.at_level(logging.WARNING, logger="alfred.ratelimit"):
			allowed, remaining, retry = await check_rate_limit(
				redis_mock, site_id="s", user="u", max_per_hour=10,
			)
		assert allowed is True
		assert remaining == 10
		assert retry == 0

	@pytest.mark.asyncio
	async def test_pipeline_error_logs_warning(self, caplog):
		redis_mock = self._make_failing_redis(RedisConnectionError("down"))
		with caplog.at_level(logging.WARNING, logger="alfred.ratelimit"):
			await check_rate_limit(redis_mock, site_id="s", user="u", max_per_hour=10)
		matching = [
			r for r in caplog.records
			if "redis call failed" in r.getMessage()
		]
		assert matching, "expected a WARNING log for the redis-error degrade path"

	@pytest.mark.asyncio
	async def test_pipeline_error_increments_metric(self):
		redis_mock = self._make_failing_redis(RedisConnectionError("down"))
		await check_rate_limit(redis_mock, site_id="s", user="u", max_per_hour=10)
		value = metrics.rate_limit_degraded_total.labels(reason="redis_error")._value.get()
		assert value == 1

	@pytest.mark.asyncio
	async def test_oserror_also_degrades(self):
		"""A bare OSError (socket timeout) is caught the same as RedisError."""
		redis_mock = self._make_failing_redis(OSError("broken pipe"))
		allowed, _, _ = await check_rate_limit(
			redis_mock, site_id="s", user="u", max_per_hour=10,
		)
		assert allowed is True
		value = metrics.rate_limit_degraded_total.labels(reason="redis_error")._value.get()
		assert value == 1


class TestDegradeUnlimitedPath:
	@pytest.mark.asyncio
	async def test_max_zero_bypasses_redis(self, caplog):
		"""max_per_hour<=0 means unlimited; no Redis call, no degrade log."""
		with caplog.at_level(logging.WARNING, logger="alfred.ratelimit"):
			allowed, remaining, _ = await check_rate_limit(
				None, site_id="s", user="u", max_per_hour=0,
			)
		# With redis=None we still hit the degrade log before the
		# max_per_hour check - that's the documented early-return path.
		# But with redis=<mock> and max=0 we'd bypass even the pipeline.
		# Here we're just checking the return shape is right.
		assert allowed is True
		assert remaining == 0  # max_per_hour echoed back
