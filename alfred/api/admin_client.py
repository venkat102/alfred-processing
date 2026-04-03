"""Admin Portal integration client for the Processing App.

Communicates with the Alfred Admin Portal for:
- Plan checking before task processing
- Usage reporting after conversations
- Site registration on first connection

Uses Redis caching for offline resilience (1-hour TTL for plan data).
"""

import json
import logging
import time

import httpx

logger = logging.getLogger("alfred.admin")

# Cache TTL for plan data
PLAN_CACHE_TTL = 3600  # 1 hour


class AdminClient:
	"""Async client for communicating with the Alfred Admin Portal."""

	def __init__(self, admin_url: str, service_api_key: str, redis=None):
		self.admin_url = admin_url.rstrip("/")
		self.service_api_key = service_api_key
		self.redis = redis
		self._headers = {"Authorization": f"Bearer {service_api_key}"}

	async def check_plan(self, site_id: str) -> dict:
		"""Check if a site is within plan limits.

		Checks Redis cache first, falls back to Admin Portal API.
		"""
		# Check cache
		if self.redis:
			cache_key = f"alfred:{site_id}:plan_cache"
			cached = await self.redis.get(cache_key)
			if cached:
				try:
					return json.loads(cached)
				except json.JSONDecodeError:
					pass

		# Call Admin Portal
		try:
			async with httpx.AsyncClient(timeout=10) as client:
				response = await client.post(
					f"{self.admin_url}/api/method/alfred_admin.api.usage.check_plan",
					json={"site_id": site_id},
					headers=self._headers,
				)
				response.raise_for_status()
				result = response.json().get("message", {})

				# Cache the result
				if self.redis:
					await self.redis.setex(cache_key, PLAN_CACHE_TTL, json.dumps(result))

				return result
		except Exception as e:
			logger.warning("Admin Portal unreachable for plan check: %s", e)
			# Fall back to cached or default allow
			return {"allowed": True, "tier": "offline", "reason": "Admin Portal unreachable - allowing by default"}

	async def report_usage(self, site_id: str, tokens: int, conversations: int, active_users: int = 1):
		"""Report usage to the Admin Portal.

		Queues in Redis if the Admin Portal is unreachable.
		"""
		payload = {
			"site_id": site_id,
			"tokens": tokens,
			"conversations": conversations,
			"active_users": active_users,
		}

		try:
			async with httpx.AsyncClient(timeout=10) as client:
				response = await client.post(
					f"{self.admin_url}/api/method/alfred_admin.api.usage.report_usage",
					json=payload,
					headers=self._headers,
				)
				response.raise_for_status()
				return response.json().get("message", {})
		except Exception as e:
			logger.warning("Failed to report usage to Admin Portal: %s - queuing for retry", e)
			# Queue for later
			if self.redis:
				await self.redis.rpush(
					f"alfred:usage_report_queue",
					json.dumps({"payload": payload, "timestamp": time.time()}),
				)
			return {"status": "queued", "error": str(e)}

	async def register_site(self, site_id: str, site_url: str = "", admin_email: str = ""):
		"""Register a customer site with the Admin Portal (idempotent)."""
		try:
			async with httpx.AsyncClient(timeout=10) as client:
				response = await client.post(
					f"{self.admin_url}/api/method/alfred_admin.api.usage.register_site",
					json={"site_id": site_id, "site_url": site_url, "admin_email": admin_email},
					headers=self._headers,
				)
				response.raise_for_status()
				return response.json().get("message", {})
		except Exception as e:
			logger.warning("Failed to register site: %s", e)
			return {"status": "error", "error": str(e)}

	async def flush_usage_queue(self):
		"""Flush queued usage reports to the Admin Portal (called periodically)."""
		if not self.redis:
			return

		queue_key = "alfred:usage_report_queue"
		flushed = 0

		while True:
			item = await self.redis.lpop(queue_key)
			if not item:
				break

			try:
				data = json.loads(item)
				payload = data["payload"]
				async with httpx.AsyncClient(timeout=10) as client:
					response = await client.post(
						f"{self.admin_url}/api/method/alfred_admin.api.usage.report_usage",
						json=payload,
						headers=self._headers,
					)
					response.raise_for_status()
					flushed += 1
			except Exception as e:
				# Put it back at the end of the queue
				await self.redis.rpush(queue_key, item)
				logger.warning("Failed to flush usage report: %s - re-queued", e)
				break

		if flushed:
			logger.info("Flushed %d queued usage reports", flushed)
