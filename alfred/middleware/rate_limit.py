"""Rate limiting middleware using Redis sliding window counters.

Limits are per-site, per-user, based on max_tasks_per_user_per_hour
sent in the handshake configuration.

Fail-open policy: when Redis is unavailable (not configured, connection
failed, or the sliding-window call raised) the check returns
``(True, max_per_hour, 0)`` so a Redis blip doesn't hard-block every
user request. This is operationally correct but makes the degraded
state invisible - both paths emit a WARNING log and increment the
``alfred_rate_limit_degraded_total`` counter so operators can alert on
sustained degradation.
"""

import logging
import time

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from alfred.obs.metrics import rate_limit_degraded_total

logger = logging.getLogger("alfred.ratelimit")

# Default rate limit if not specified
DEFAULT_MAX_TASKS_PER_HOUR = 20


async def check_rate_limit(
	redis: aioredis.Redis | None,
	site_id: str,
	user: str,
	max_per_hour: int = DEFAULT_MAX_TASKS_PER_HOUR,
	source: str = "unknown",
) -> tuple[bool, int, int]:
	"""Check if a user has exceeded their rate limit.

	Uses a Redis sorted set with timestamps as scores for a sliding window.

	Args:
		redis: Redis client (if None, rate limiting is disabled).
		site_id: Customer site identifier.
		user: User email.
		max_per_hour: Maximum tasks per user per hour. 0 means unlimited.
		source: "rest" or "websocket" — tagged onto the Prometheus block
			counter so operators can see which entry path is being abused.

	Returns:
		Tuple of (allowed: bool, remaining: int, retry_after_seconds: int).
	"""
	if redis is None:
		# No Redis client configured. Fail-open (allow), but record the
		# degradation so operators can alert on a sustained rate of this
		# state. Logged at WARNING (not DEBUG) because rate limiting is
		# silently off - that's an ops event, not a dev event.
		logger.warning(
			"rate limit: no redis client, allowing request for %s@%s (degraded)",
			user, site_id,
		)
		rate_limit_degraded_total.labels(reason="no_client").inc()
		return True, max_per_hour, 0

	if max_per_hour <= 0:
		return True, -1, 0  # Unlimited

	key = f"alfred:{site_id}:ratelimit:{user}"
	now = time.time()
	window_start = now - 3600  # 1 hour sliding window

	try:
		pipe = redis.pipeline()
		# Remove entries older than 1 hour
		pipe.zremrangebyscore(key, "-inf", window_start)
		# Count entries in the current window
		pipe.zcard(key)
		# Add the current request
		pipe.zadd(key, {f"{now}": now})
		# Set TTL so the key auto-expires
		pipe.expire(key, 3600)
		results = await pipe.execute()
	except (RedisError, OSError, ConnectionError) as e:
		# Redis disappeared mid-call (restart, network blip, pool
		# exhausted). Fail-open - a transient Redis issue must not
		# manifest as a hard block on every user. Same observability
		# contract as the no-client branch: log + metric.
		logger.warning(
			"rate limit: redis call failed, allowing request for %s@%s (%s: %s)",
			user, site_id, type(e).__name__, e,
		)
		rate_limit_degraded_total.labels(reason="redis_error").inc()
		return True, max_per_hour, 0

	current_count = results[1]

	remaining = max(0, max_per_hour - current_count - 1)

	if current_count >= max_per_hour:
		# Rate limit exceeded - calculate retry-after
		try:
			oldest_entries = await redis.zrange(key, 0, 0, withscores=True)
			if oldest_entries:
				oldest_time = oldest_entries[0][1]
				retry_after = int(oldest_time + 3600 - now) + 1
			else:
				retry_after = 60

			# Remove the entry we just added since the request is denied
			await redis.zrem(key, f"{now}")
		except (RedisError, OSError, ConnectionError):
			# Couldn't compute a precise retry-after / couldn't undo
			# the just-added entry. Still correct to deny the request -
			# we already have confirmation the window was full.
			retry_after = 60
		logger.warning(
			"Rate limit exceeded for %s@%s (%d/%d) source=%s",
			user, site_id, current_count, max_per_hour, source,
		)
		# Metric increment is best-effort; a broken metrics import must
		# not block the rate-limit response from reaching the user.
		try:
			from alfred.obs.metrics import rate_limit_block_total
			rate_limit_block_total.labels(source=source).inc()
		except Exception:  # noqa: BLE001 — metrics best-effort; must not block the rate-limit response from reaching the user
			pass
		return False, 0, retry_after

	return True, remaining, 0
