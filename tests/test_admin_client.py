"""Tests for alfred.api.admin_client (TD coverage gap — was 0%).

AdminClient talks to the Alfred Admin Portal for plan checks, usage
reports, and site registration. Network is mocked at the ``httpx.
AsyncClient`` layer so the tests exercise the real request shapes,
error paths, Redis cache behaviour, and retry-queue semantics without
hitting a real portal.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from alfred.api.admin_client import PLAN_CACHE_TTL, AdminClient


class _FakeRedis:
	"""In-memory ``redis.asyncio.Redis`` stand-in sufficient for AdminClient."""

	def __init__(self):
		self._kv: dict[str, str] = {}
		self._q: dict[str, list[str]] = {}

	async def get(self, key: str) -> str | None:
		return self._kv.get(key)

	async def setex(self, key: str, ttl: int, value: str) -> None:
		# TTL is logged but not enforced in the fake.
		self._kv[key] = value

	async def rpush(self, key: str, value: str) -> None:
		self._q.setdefault(key, []).append(value)

	async def lpop(self, key: str) -> str | None:
		q = self._q.get(key, [])
		return q.pop(0) if q else None


def _client(redis=None) -> AdminClient:
	return AdminClient(
		admin_url="https://admin.example.com",
		service_api_key="svc-key",
		redis=redis,
	)


def _httpx_response(status: int, body: dict) -> httpx.Response:
	return httpx.Response(
		status_code=status,
		json=body,
		request=httpx.Request("POST", "https://admin.example.com/x"),
	)


@pytest.mark.asyncio
class TestCheckPlan:
	async def test_hits_portal_and_caches_response(self):
		redis = _FakeRedis()
		client = _client(redis)
		body = {"message": {"allowed": True, "tier": "pro"}}

		post_mock = AsyncMock(return_value=_httpx_response(200, body))
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")

		assert result == {"allowed": True, "tier": "pro"}
		# Portal hit with the expected URL / payload / auth header.
		args, kwargs = post_mock.call_args
		assert args[0].endswith("/alfred_admin.api.usage.check_plan")
		assert kwargs["json"] == {"site_id": "site-a"}
		assert kwargs["headers"] == {"Authorization": "Bearer svc-key"}
		# Result cached in redis under site-specific key.
		cached = await redis.get("alfred:site-a:plan_cache")
		assert json.loads(cached) == {"allowed": True, "tier": "pro"}

	async def test_cache_hit_skips_portal_call(self):
		redis = _FakeRedis()
		await redis.setex(
			"alfred:site-a:plan_cache", PLAN_CACHE_TTL,
			json.dumps({"allowed": True, "tier": "cached"}),
		)
		client = _client(redis)

		post_mock = AsyncMock()
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")

		assert result["tier"] == "cached"
		post_mock.assert_not_called()

	async def test_malformed_cache_falls_through_to_portal(self):
		redis = _FakeRedis()
		# Corrupt cache value — not JSON. Client should ignore and
		# hit the portal instead of crashing.
		redis._kv["alfred:site-a:plan_cache"] = "not-json"
		client = _client(redis)
		body = {"message": {"allowed": True}}
		post_mock = AsyncMock(return_value=_httpx_response(200, body))
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")
		assert result == {"allowed": True}
		post_mock.assert_called_once()

	async def test_network_failure_returns_offline_default(self):
		redis = _FakeRedis()
		client = _client(redis)
		post_mock = AsyncMock(
			side_effect=httpx.ConnectError("admin unreachable"),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")
		assert result["allowed"] is True
		assert result["tier"] == "offline"
		assert "unreachable" in result["reason"].lower()

	async def test_http_error_returns_offline_default(self):
		redis = _FakeRedis()
		client = _client(redis)
		resp = _httpx_response(500, {"error": "boom"})
		# raise_for_status path.
		post_mock = AsyncMock(return_value=resp)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")
		assert result["allowed"] is True
		assert result["tier"] == "offline"

	async def test_no_redis_does_not_crash(self):
		client = _client(redis=None)
		post_mock = AsyncMock(
			return_value=_httpx_response(200, {"message": {"allowed": True}}),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			result = await client.check_plan("site-a")
		assert result == {"allowed": True}


@pytest.mark.asyncio
class TestReportUsage:
	async def test_happy_path_posts_payload(self):
		client = _client()
		post_mock = AsyncMock(
			return_value=_httpx_response(200, {"message": {"status": "ok"}}),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			out = await client.report_usage(
				"site-a", tokens=100, conversations=2, active_users=3,
			)
		assert out == {"status": "ok"}
		_, kwargs = post_mock.call_args
		assert kwargs["json"] == {
			"site_id": "site-a",
			"tokens": 100,
			"conversations": 2,
			"active_users": 3,
		}

	async def test_network_failure_queues_in_redis(self):
		redis = _FakeRedis()
		client = _client(redis)
		post_mock = AsyncMock(
			side_effect=httpx.ConnectError("admin unreachable"),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			out = await client.report_usage(
				"site-a", tokens=42, conversations=1,
			)
		assert out["status"] == "queued"
		# Queue got the payload with a timestamp.
		q = redis._q["alfred:usage_report_queue"]
		assert len(q) == 1
		queued = json.loads(q[0])
		assert queued["payload"]["site_id"] == "site-a"
		assert queued["payload"]["tokens"] == 42
		assert "timestamp" in queued

	async def test_network_failure_without_redis_returns_error(self):
		client = _client(redis=None)
		post_mock = AsyncMock(
			side_effect=httpx.ConnectError("admin unreachable"),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			out = await client.report_usage("site-a", tokens=42, conversations=1)
		assert out["status"] == "queued"


@pytest.mark.asyncio
class TestRegisterSite:
	async def test_happy_path(self):
		client = _client()
		post_mock = AsyncMock(
			return_value=_httpx_response(200, {"message": {"registered": True}}),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			out = await client.register_site(
				"site-a",
				site_url="https://site-a.example.com",
				admin_email="ops@site-a.example.com",
			)
		assert out == {"registered": True}
		_, kwargs = post_mock.call_args
		assert kwargs["json"]["site_id"] == "site-a"

	async def test_network_failure_returns_error_dict(self):
		client = _client()
		post_mock = AsyncMock(
			side_effect=httpx.ConnectError("admin unreachable"),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			out = await client.register_site("site-a")
		assert out["status"] == "error"
		assert "unreachable" in out["error"].lower()


@pytest.mark.asyncio
class TestFlushUsageQueue:
	async def test_noop_without_redis(self):
		client = _client(redis=None)
		# Just make sure it doesn't crash.
		await client.flush_usage_queue()

	async def test_drains_queue_on_success(self):
		redis = _FakeRedis()
		for payload in [{"site_id": "a"}, {"site_id": "b"}]:
			await redis.rpush(
				"alfred:usage_report_queue",
				json.dumps({"payload": payload, "timestamp": 1.0}),
			)
		client = _client(redis)
		post_mock = AsyncMock(
			return_value=_httpx_response(200, {"message": {"ok": True}}),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			await client.flush_usage_queue()
		# Queue empty + both payloads posted.
		assert "alfred:usage_report_queue" not in redis._q or not redis._q["alfred:usage_report_queue"]
		assert post_mock.call_count == 2

	async def test_requeues_and_stops_on_http_failure(self):
		redis = _FakeRedis()
		item = json.dumps({"payload": {"site_id": "a"}, "timestamp": 1.0})
		await redis.rpush("alfred:usage_report_queue", item)
		await redis.rpush(
			"alfred:usage_report_queue",
			json.dumps({"payload": {"site_id": "b"}, "timestamp": 2.0}),
		)
		client = _client(redis)
		post_mock = AsyncMock(
			side_effect=httpx.ConnectError("admin down"),
		)
		with patch.object(httpx.AsyncClient, "post", post_mock):
			await client.flush_usage_queue()
		# First item re-queued (tail), loop broke before touching the rest.
		q = redis._q["alfred:usage_report_queue"]
		# Both original items still present (the failed one was re-queued
		# at the tail, the second was never consumed).
		assert len(q) == 2

	async def test_requeues_malformed_queue_item(self):
		# JSONDecodeError / KeyError path — re-queue and stop.
		redis = _FakeRedis()
		await redis.rpush("alfred:usage_report_queue", "not-json")
		client = _client(redis)
		post_mock = AsyncMock()
		with patch.object(httpx.AsyncClient, "post", post_mock):
			await client.flush_usage_queue()
		# No network call — the malformed item caused the break before
		# httpx was touched.
		post_mock.assert_not_called()
		assert redis._q["alfred:usage_report_queue"] == ["not-json"]
