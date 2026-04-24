"""Rate limiting middleware using Redis sliding window counters.

Limits are per-site, per-user, based on max_tasks_per_user_per_hour
sent in the handshake configuration.
"""

import logging
import time

import redis.asyncio as aioredis

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
		return True, max_per_hour, 0

	if max_per_hour <= 0:
		return True, -1, 0  # Unlimited

	key = f"alfred:{site_id}:ratelimit:{user}"
	now = time.time()
	window_start = now - 3600  # 1 hour sliding window

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
	current_count = results[1]

	remaining = max(0, max_per_hour - current_count - 1)

	if current_count >= max_per_hour:
		# Rate limit exceeded - calculate retry-after
		oldest_entries = await redis.zrange(key, 0, 0, withscores=True)
		if oldest_entries:
			oldest_time = oldest_entries[0][1]
			retry_after = int(oldest_time + 3600 - now) + 1
		else:
			retry_after = 60

		# Remove the entry we just added since the request is denied
		await redis.zrem(key, f"{now}")
		logger.warning("Rate limit exceeded for %s@%s (%d/%d) source=%s", user, site_id, current_count, max_per_hour, source)
		# Metric increment is best-effort; a broken metrics import must
		# not block the rate-limit response from reaching the user.
		try:
			from alfred.obs.metrics import rate_limit_block_total
			rate_limit_block_total.labels(source=source).inc()
		except Exception:  # noqa: BLE001 — metrics best-effort; must not block the rate-limit response from reaching the user
			pass
		return False, 0, retry_after

	return True, remaining, 0
