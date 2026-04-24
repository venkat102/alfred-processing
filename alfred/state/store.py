"""Redis-backed state store for Alfred Processing App.

All state is namespaced by site_id to ensure multi-tenant isolation.

Key schema:
	alfred:{site_id}:task:{task_id}      - Hash storing task state as JSON
	alfred:{site_id}:events:{conv_id}    - Stream of real-time events per conversation
	alfred:{site_id}:cache:{key}         - TTL-based cached data (plan limits, site context)
"""

import json
import logging
import re
from typing import Any

import redis.asyncio as aioredis

logger = logging.getLogger("alfred.state")

# Maximum events per conversation stream to prevent unbounded growth
DEFAULT_STREAM_MAXLEN = 10_000

# Default key-level TTL for event streams. Refreshed on every push, so an
# active conversation's stream stays indefinitely while a conversation
# idle for >7 days auto-expires. Caps Redis memory against the pathology
# of "every conversation ever created, kept forever even after abandoned"
# - maxlen alone only bounds entries-per-stream, not stream count.
DEFAULT_STREAM_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Regex for valid identifiers (alphanumeric, hyphens, dots, underscores)
_VALID_ID = re.compile(r"^[a-zA-Z0-9._-]+$")


def _validate_id(value: str, name: str) -> None:
	"""Validate that an identifier is safe for use in Redis keys."""
	if not value:
		raise ValueError(f"{name} cannot be empty")
	if not _VALID_ID.match(value):
		raise ValueError(f"{name} contains invalid characters: {value!r}")


class StateStore:
	"""Redis-backed state store with multi-tenant namespace isolation.

	All operations are async and use the redis.asyncio client.
	Every key is prefixed with `alfred:{site_id}:` to ensure complete
	isolation between customer sites.
	"""

	def __init__(
		self,
		redis: aioredis.Redis,
		stream_maxlen: int = DEFAULT_STREAM_MAXLEN,
		stream_ttl_seconds: int = DEFAULT_STREAM_TTL_SECONDS,
	):
		self._redis = redis
		self._stream_maxlen = stream_maxlen
		# TTL of 0 disables auto-expiry (opt-out for tests or specialised
		# deployments that want permanent event retention); negative values
		# are treated the same. Normal runs use the 7-day default.
		self._stream_ttl_seconds = stream_ttl_seconds if stream_ttl_seconds > 0 else 0

	def _key(self, site_id: str, *parts: str) -> str:
		"""Build a namespaced Redis key."""
		_validate_id(site_id, "site_id")
		return ":".join(["alfred", site_id, *parts])

	# ── Task State CRUD ──────────────────────────────────────────────

	async def set_task_state(self, site_id: str, task_id: str, state_dict: dict[str, Any]) -> None:
		"""Store or update task state as JSON.

		Args:
			site_id: Customer site identifier.
			task_id: Unique task identifier.
			state_dict: Serializable dict representing the task state.

		Raises:
			ValueError: If site_id or task_id is invalid.
			TypeError: If state_dict is not JSON-serializable.
			redis.exceptions.ConnectionError: If Redis is unavailable.
		"""
		_validate_id(task_id, "task_id")
		key = self._key(site_id, "task", task_id)
		try:
			value = json.dumps(state_dict)
		except (TypeError, ValueError) as e:
			raise TypeError(f"state_dict is not JSON-serializable: {e}") from e

		await self._redis.set(key, value)
		logger.debug("Set task state: %s", key)

	async def get_task_state(self, site_id: str, task_id: str) -> dict[str, Any] | None:
		"""Retrieve task state.

		Returns:
			The task state dict, or None if the task doesn't exist.
		"""
		_validate_id(task_id, "task_id")
		key = self._key(site_id, "task", task_id)
		value = await self._redis.get(key)
		if value is None:
			return None
		return json.loads(value)

	async def delete_task_state(self, site_id: str, task_id: str) -> bool:
		"""Delete task state. Returns True if the key existed.

		Returns:
			True if the task was deleted, False if it didn't exist.
		"""
		_validate_id(task_id, "task_id")
		key = self._key(site_id, "task", task_id)
		deleted = await self._redis.delete(key)
		logger.debug("Deleted task state: %s (existed=%s)", key, bool(deleted))
		return bool(deleted)

	# ── Event Stream ─────────────────────────────────────────────────

	async def push_event(self, site_id: str, conversation_id: str, event: dict[str, Any]) -> str:
		"""Append an event to a conversation's Redis Stream.

		Args:
			site_id: Customer site identifier.
			conversation_id: Conversation identifier.
			event: Event data dict. Must be JSON-serializable.

		Returns:
			The stream entry ID assigned by Redis.

		Raises:
			ValueError: If identifiers are invalid.
			TypeError: If event is not JSON-serializable.
		"""
		_validate_id(conversation_id, "conversation_id")
		key = self._key(site_id, "events", conversation_id)

		try:
			event_json = json.dumps(event)
		except (TypeError, ValueError) as e:
			raise TypeError(f"event is not JSON-serializable: {e}") from e

		entry_id = await self._redis.xadd(
			key,
			{"data": event_json},
			maxlen=self._stream_maxlen,
		)
		# Refresh key-level TTL on every push. An active conversation that
		# keeps emitting events stays alive; a stream that goes silent for
		# stream_ttl_seconds gets reaped automatically. Cheap second call
		# to the same Redis node; no pipelining to avoid tangling the
		# xadd return value we just promised the caller.
		if self._stream_ttl_seconds > 0:
			try:
				await self._redis.expire(key, self._stream_ttl_seconds)
			except Exception as e:
				# Expiry refresh is best-effort; a Redis hiccup here must
				# not fail the event push itself. Worst case the stream
				# keeps the previous TTL (or none), which is the
				# pre-feature behaviour.
				logger.debug("Failed to refresh stream TTL for %s: %s", key, e)
		logger.debug("Pushed event to %s: id=%s", key, entry_id)
		return entry_id

	async def get_events(
		self, site_id: str, conversation_id: str, since_id: str = "0"
	) -> list[dict[str, Any]]:
		"""Read events from a conversation stream since a given ID.

		Args:
			site_id: Customer site identifier.
			conversation_id: Conversation identifier.
			since_id: Stream ID to read from (exclusive). Use "0" for all events.

		Returns:
			List of dicts: [{"id": "...", "data": {...}}, ...]
		"""
		_validate_id(conversation_id, "conversation_id")
		key = self._key(site_id, "events", conversation_id)

		try:
			entries = await self._redis.xrange(key, min=f"({since_id}" if since_id != "0" else "-", max="+")
		except Exception:
			# If since_id is invalid or stream doesn't exist, return from beginning
			logger.warning("Failed to read from %s since %s, reading from start", key, since_id)
			entries = await self._redis.xrange(key, min="-", max="+")

		result = []
		for entry_id, fields in entries:
			data_str = fields.get("data", "{}")
			try:
				data = json.loads(data_str)
			except json.JSONDecodeError:
				data = {"raw": data_str}
			result.append({"id": entry_id, "data": data})

		return result

	# ── TTL-based Cache ──────────────────────────────────────────────

	async def set_with_ttl(self, site_id: str, key: str, value: str, ttl_seconds: int) -> None:
		"""Store a value with automatic expiration.

		Args:
			site_id: Customer site identifier.
			key: Cache key (will be prefixed with site namespace).
			value: String value to store.
			ttl_seconds: Time-to-live in seconds.

		Raises:
			ValueError: If ttl_seconds is not positive.
		"""
		if ttl_seconds <= 0:
			raise ValueError("ttl_seconds must be positive")
		_validate_id(key, "cache key")
		full_key = self._key(site_id, "cache", key)
		await self._redis.setex(full_key, ttl_seconds, value)
		logger.debug("Set with TTL: %s (ttl=%ds)", full_key, ttl_seconds)

	async def get_cached(self, site_id: str, key: str) -> str | None:
		"""Retrieve a cached value. Returns None if expired or missing."""
		_validate_id(key, "cache key")
		full_key = self._key(site_id, "cache", key)
		return await self._redis.get(full_key)

	# ── Health Check ─────────────────────────────────────────────────

	async def is_healthy(self) -> bool:
		"""Check if Redis is reachable by sending a PING command."""
		try:
			return await self._redis.ping()
		except Exception as e:
			logger.error("Redis health check failed: %s", e)
			return False
