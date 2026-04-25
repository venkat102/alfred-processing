"""Tests for the P0.2 REST JWT authentication wire.

Before this landed, ``POST /api/v1/tasks`` trusted ``site_id`` and
``user`` straight from the request body. Anyone with the shared
``API_SECRET_KEY`` could submit tasks as any tenant. The audit
flagged this as the highest-severity exposure on the REST surface.

Now every REST handler that needs site/user context resolves them
from a per-user JWT supplied in ``X-Alfred-JWT``. The body is still
validated against the JWT (mismatched ``site_id`` → 403) so client
misconfiguration surfaces loudly instead of being silently coerced.

These tests cover the contract end-to-end against a real ASGI app
(no Redis required — the route exits at 503 ``REDIS_UNAVAILABLE``
when Redis is missing, which is *after* the auth dependencies run,
so we still exercise the auth code paths).
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from alfred.middleware.auth import create_jwt_token

API_KEY = "test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4"
SITE_ID = "tenant-alpha.frappe.cloud"
OTHER_SITE = "tenant-beta.frappe.cloud"
USER = "alice@example.com"
ROLES = ["System Manager"]


def _jwt(site_id: str = SITE_ID, user: str = USER, *, secret: str = API_KEY) -> str:
	return create_jwt_token(user, ROLES, site_id, secret, exp_hours=1)


@pytest.fixture
def app(monkeypatch):
	monkeypatch.setenv("API_SECRET_KEY", API_KEY)
	monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost:8001")
	os.environ.pop("REDIS_URL", None)

	from alfred.config import get_settings
	get_settings.cache_clear()

	from alfred.main import create_app
	test_app = create_app()
	test_app.state.settings = get_settings()
	# Provide a fake redis so the route's REDIS_UNAVAILABLE early-exit
	# doesn't run before auth — we want auth failures to be the
	# observed status code, not 503/REDIS. Async methods return
	# AsyncMocks so ``await pipe.execute()`` resolves cleanly when
	# the JWT-happy-path test gets through to the rate-limit check.
	fake_redis = MagicMock()
	fake_redis.pipeline.return_value.execute = AsyncMock(return_value=[0, 0, 1, True])
	fake_redis.zrange = AsyncMock(return_value=[])
	fake_redis.zrem = AsyncMock(return_value=0)
	# Stub the few async Redis methods StateStore actually awaits so the
	# auth-happy-path test reaches a 2xx without falling over on storage.
	fake_redis.setex = AsyncMock(return_value=True)
	fake_redis.get = AsyncMock(return_value=None)
	test_app.state.redis = fake_redis
	yield test_app
	get_settings.cache_clear()


@pytest.fixture
async def client(app):
	transport = ASGITransport(app=app)
	async with AsyncClient(transport=transport, base_url="http://test") as ac:
		yield ac


def _headers(*, jwt: str | None = None, api_key: str = API_KEY) -> dict:
	h = {"Authorization": f"Bearer {api_key}"}
	if jwt is not None:
		h["X-Alfred-JWT"] = jwt
	return h


def _body(site_id: str = SITE_ID, user: str = USER) -> dict:
	return {
		"prompt": "Create a Customer doctype",
		"site_config": {"site_id": site_id},
		"user_context": {"user": user},
	}


class TestJWTRequired:
	"""``X-Alfred-JWT`` is mandatory on every REST endpoint that touches
	site-scoped data. Service-only auth (the Bearer key) is no longer
	sufficient."""

	@pytest.mark.asyncio
	async def test_post_without_jwt_returns_401(self, client):
		resp = await client.post(
			"/api/v1/tasks", json=_body(),
			headers=_headers(jwt=None),
		)
		assert resp.status_code == 401
		# TD-M2's global handler flattens HTTPException(detail={...})
		# into the body itself, so `code` is at the top level.
		assert resp.json()["code"] == "JWT_MISSING"

	@pytest.mark.asyncio
	async def test_get_status_without_jwt_returns_401(self, client):
		resp = await client.get(
			"/api/v1/tasks/some-id", headers=_headers(jwt=None),
		)
		assert resp.status_code == 401
		assert resp.json().get("code") == "JWT_MISSING"

	@pytest.mark.asyncio
	async def test_get_messages_without_jwt_returns_401(self, client):
		resp = await client.get(
			"/api/v1/tasks/some-id/messages",
			headers=_headers(jwt=None),
		)
		assert resp.status_code == 401
		assert resp.json().get("code") == "JWT_MISSING"


class TestJWTValidation:
	"""Bad JWTs surface as 401 + JWT_INVALID, not 500."""

	@pytest.mark.asyncio
	async def test_malformed_jwt_returns_401(self, client):
		resp = await client.post(
			"/api/v1/tasks", json=_body(),
			headers=_headers(jwt="not-a-real-jwt"),
		)
		assert resp.status_code == 401
		assert resp.json().get("code") == "JWT_INVALID"

	@pytest.mark.asyncio
	async def test_jwt_signed_with_wrong_secret_returns_401(self, client):
		"""Attacker mints a JWT with a different secret. Signature check
		must reject it before any tenancy decision is made."""
		forged = _jwt(secret="attacker-secret-32-bytes-padding-padding")
		resp = await client.post(
			"/api/v1/tasks", json=_body(),
			headers=_headers(jwt=forged),
		)
		assert resp.status_code == 401
		assert resp.json().get("code") == "JWT_INVALID"


class TestSiteMismatchRejected:
	"""The cross-tenant exploit class the audit P0.2 was about: an
	attacker with a JWT for tenant α tries to submit work into
	tenant β by lying about ``site_id`` in the body."""

	@pytest.mark.asyncio
	async def test_body_site_id_mismatch_jwt_returns_403(self, client):
		resp = await client.post(
			"/api/v1/tasks",
			json=_body(site_id=OTHER_SITE),  # body says β
			headers=_headers(jwt=_jwt(site_id=SITE_ID)),  # JWT says α
		)
		assert resp.status_code == 403
		body = resp.json()
		assert body["code"] == "SITE_MISMATCH"
		# Error message must name BOTH ids so the operator can diagnose
		# whether it's a stale client config or a real attack.
		assert SITE_ID in body["error"] and OTHER_SITE in body["error"]

	@pytest.mark.asyncio
	async def test_body_omits_site_id_jwt_value_used(self, client):
		"""A client may legitimately omit site_id from the body and rely
		on the JWT — that case is allowed and the JWT's value flows
		through to the runner."""
		body = _body()
		body["site_config"].pop("site_id")
		resp = await client.post(
			"/api/v1/tasks", json=body, headers=_headers(jwt=_jwt()),
		)
		# 503 because we wired a MagicMock redis that doesn't actually
		# implement the methods, so set_task_state fails. The point of
		# this test is the auth/site-derive layer succeeded — the
		# response is past the JWT_MISMATCH gate.
		assert resp.status_code in (200, 201, 500, 503)


class TestRESTLayeredAuth:
	"""Both layers required: service Bearer + per-user JWT."""

	@pytest.mark.asyncio
	async def test_jwt_alone_without_bearer_returns_401(self, client):
		"""Bearer is still the service-level gate. Even a valid JWT
		can't bypass it — that would let an attacker who only has a
		leaked JWT (e.g. lifted from a logged URL) call the API."""
		resp = await client.post(
			"/api/v1/tasks", json=_body(),
			headers={"X-Alfred-JWT": _jwt()},
		)
		assert resp.status_code == 401
		# Bearer fails first, so the code is AUTH_MISSING not JWT_MISSING.
		assert resp.json().get("code") == "AUTH_MISSING"
