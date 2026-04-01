"""Tests for the API Gateway — REST endpoints, auth, rate limiting, WebSocket."""

import json
import os

import jwt
import pytest
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient

from intern.main import create_app
from intern.middleware.auth import create_jwt_token, verify_jwt_token

API_KEY = "test-secret-key-12345"
SITE_ID = "test.frappe.cloud"
USER = "admin@test.com"
ROLES = ["System Manager", "Administrator"]
REDIS_URL = "redis://127.0.0.1:11000/2"


@pytest.fixture
async def app():
	"""Create a FastAPI app with initialized state (simulating lifespan)."""
	os.environ["API_SECRET_KEY"] = API_KEY
	os.environ["REDIS_URL"] = REDIS_URL
	test_app = create_app()

	# Manually init state (lifespan doesn't run with ASGITransport)
	from intern.config import get_settings

	test_app.state.settings = get_settings()
	try:
		pool = aioredis.ConnectionPool.from_url(REDIS_URL, max_connections=5, decode_responses=True)
		test_app.state.redis = aioredis.Redis(connection_pool=pool)
		await test_app.state.redis.ping()
	except Exception:
		test_app.state.redis = None

	yield test_app

	# Cleanup: flush test keys
	if test_app.state.redis:
		keys = []
		async for key in test_app.state.redis.scan_iter("alfred:*"):
			keys.append(key)
		if keys:
			await test_app.state.redis.delete(*keys)
		await test_app.state.redis.aclose()


@pytest.fixture
async def client(app):
	transport = ASGITransport(app=app)
	async with AsyncClient(transport=transport, base_url="http://test") as ac:
		yield ac


def _auth_headers():
	return {"Authorization": f"Bearer {API_KEY}"}


def _make_jwt():
	return create_jwt_token(USER, ROLES, SITE_ID, API_KEY, exp_hours=1)


# ── Health Endpoint (no auth) ────────────────────────────────────


class TestHealth:
	async def test_health_returns_ok(self, client):
		resp = await client.get("/health")
		assert resp.status_code == 200
		data = resp.json()
		assert data["status"] == "ok"
		assert "version" in data

	async def test_health_no_auth_required(self, client):
		resp = await client.get("/health")
		assert resp.status_code == 200


# ── API Key Authentication ───────────────────────────────────────


class TestAuth:
	async def test_missing_auth_returns_401(self, client):
		resp = await client.post("/api/v1/tasks", json={"prompt": "test"})
		assert resp.status_code == 401
		assert resp.json()["detail"]["code"] == "AUTH_MISSING"

	async def test_invalid_key_returns_401(self, client):
		resp = await client.post(
			"/api/v1/tasks",
			json={"prompt": "test"},
			headers={"Authorization": "Bearer wrong-key"},
		)
		assert resp.status_code == 401
		assert resp.json()["detail"]["code"] == "AUTH_INVALID"

	async def test_valid_key_passes(self, client, app):
		resp = await client.post(
			"/api/v1/tasks",
			json={
				"prompt": "Create a ToDo",
				"site_config": {"site_id": SITE_ID, "max_tasks_per_user_per_hour": 100},
				"user_context": {"user": USER},
			},
			headers=_auth_headers(),
		)
		if app.state.redis is None:
			assert resp.status_code == 503
		else:
			assert resp.status_code == 201


# ── JWT Verification ─────────────────────────────────────────────


class TestJWT:
	def test_valid_jwt(self):
		token = create_jwt_token(USER, ROLES, SITE_ID, API_KEY)
		payload = verify_jwt_token(token, API_KEY)
		assert payload["user"] == USER
		assert payload["roles"] == ROLES
		assert payload["site_id"] == SITE_ID

	def test_expired_jwt(self):
		token = create_jwt_token(USER, ROLES, SITE_ID, API_KEY, exp_hours=-1)
		with pytest.raises(ValueError, match="expired"):
			verify_jwt_token(token, API_KEY)

	def test_tampered_jwt(self):
		token = create_jwt_token(USER, ROLES, SITE_ID, API_KEY)
		parts = token.split(".")
		parts[1] = parts[1] + "x"
		tampered = ".".join(parts)
		with pytest.raises(ValueError):
			verify_jwt_token(tampered, API_KEY)

	def test_wrong_secret(self):
		token = create_jwt_token(USER, ROLES, SITE_ID, "different-secret")
		with pytest.raises(ValueError, match="signature"):
			verify_jwt_token(token, API_KEY)

	def test_missing_claims(self):
		payload = {"user": USER}
		token = jwt.encode(payload, API_KEY, algorithm="HS256")
		with pytest.raises(ValueError, match="missing required claims"):
			verify_jwt_token(token, API_KEY)

	def test_empty_site_id(self):
		payload = {"user": USER, "roles": ROLES, "site_id": ""}
		token = jwt.encode(payload, API_KEY, algorithm="HS256")
		with pytest.raises(ValueError, match="site_id claim cannot be empty"):
			verify_jwt_token(token, API_KEY)


# ── Task CRUD Endpoints ─────────────────────────────────────────


class TestTaskEndpoints:
	async def test_create_task(self, client, app):
		if app.state.redis is None:
			pytest.skip("Redis not available")

		resp = await client.post(
			"/api/v1/tasks",
			json={
				"prompt": "Create a ToDo DocType",
				"site_config": {"site_id": SITE_ID, "max_tasks_per_user_per_hour": 100},
				"user_context": {"user": USER, "roles": ROLES},
			},
			headers=_auth_headers(),
		)
		assert resp.status_code == 201
		data = resp.json()
		assert "task_id" in data
		assert data["status"] == "queued"

	async def test_get_task_status(self, client, app):
		if app.state.redis is None:
			pytest.skip("Redis not available")

		# Create first
		create_resp = await client.post(
			"/api/v1/tasks",
			json={
				"prompt": "test task",
				"site_config": {"site_id": SITE_ID, "max_tasks_per_user_per_hour": 100},
				"user_context": {"user": USER},
			},
			headers=_auth_headers(),
		)
		task_id = create_resp.json()["task_id"]

		resp = await client.get(
			f"/api/v1/tasks/{task_id}?site_id={SITE_ID}",
			headers=_auth_headers(),
		)
		assert resp.status_code == 200
		assert resp.json()["task_id"] == task_id
		assert resp.json()["status"] == "queued"

	async def test_get_nonexistent_task(self, client, app):
		if app.state.redis is None:
			pytest.skip("Redis not available")

		resp = await client.get(
			"/api/v1/tasks/nonexistent-id?site_id=x",
			headers=_auth_headers(),
		)
		assert resp.status_code == 404

	async def test_get_task_messages(self, client, app):
		if app.state.redis is None:
			pytest.skip("Redis not available")

		resp = await client.get(
			"/api/v1/tasks/some-task/messages?site_id=test",
			headers=_auth_headers(),
		)
		assert resp.status_code == 200
		assert isinstance(resp.json(), list)


# ── Rate Limiting ────────────────────────────────────────────────


class TestRateLimit:
	async def test_rate_limit_exceeded(self, client, app):
		if app.state.redis is None:
			pytest.skip("Redis not available")

		# Server uses SERVER_DEFAULT_RATE_LIMIT (20). Send 21 requests to exceed it.
		# For faster testing, temporarily set the server default lower.
		import intern.api.routes as routes_mod
		original_limit = routes_mod.SERVER_DEFAULT_RATE_LIMIT
		routes_mod.SERVER_DEFAULT_RATE_LIMIT = 2

		try:
			for i in range(3):
				resp = await client.post(
					"/api/v1/tasks",
					json={
						"prompt": f"task {i}",
						"site_config": {"site_id": "ratelimit-test-site-v2"},
						"user_context": {"user": "rate-v2@test.com"},
					},
					headers=_auth_headers(),
				)
				if i < 2:
					assert resp.status_code == 201, f"Request {i} should succeed: {resp.text}"
				else:
					assert resp.status_code == 429, f"Request {i} should be rate limited: {resp.text}"
					assert "Retry-After" in resp.headers
		finally:
			routes_mod.SERVER_DEFAULT_RATE_LIMIT = original_limit


# ── WebSocket ────────────────────────────────────────────────────


class TestWebSocket:
	async def test_ws_valid_handshake(self, app):
		try:
			from httpx_ws import aconnect_ws
			from httpx_ws.transport import ASGIWebSocketTransport
		except ImportError:
			pytest.skip("httpx_ws not installed")

		async with AsyncClient(
			transport=ASGIWebSocketTransport(app=app),
			base_url="http://test",
		) as ws_client:
			async with aconnect_ws("/ws/test-conv-auth", ws_client) as ws:
				await ws.send_json({"api_key": API_KEY, "jwt_token": _make_jwt(), "site_config": {}})
				resp = json.loads(await ws.receive_text())
				assert resp["type"] == "auth_success"
				assert resp["data"]["user"] == USER
				assert resp["data"]["site_id"] == SITE_ID

	async def test_ws_message_routing_custom(self, app):
		try:
			from httpx_ws import aconnect_ws
			from httpx_ws.transport import ASGIWebSocketTransport
		except ImportError:
			pytest.skip("httpx_ws not installed")

		async with AsyncClient(
			transport=ASGIWebSocketTransport(app=app),
			base_url="http://test",
		) as ws_client:
			async with aconnect_ws("/ws/test-conv-custom", ws_client) as ws:
				await ws.send_json({"api_key": API_KEY, "jwt_token": _make_jwt()})
				auth = json.loads(await ws.receive_text())
				assert auth["type"] == "auth_success"

				# Send a non-prompt custom message (prompt triggers the pipeline which needs a real LLM)
				await ws.send_json({"msg_id": "msg-001", "type": "status_query", "data": {"text": "what is happening"}})

				# Read responses, skip pings
				resp = json.loads(await ws.receive_text())
				while resp.get("type") == "ping":
					resp = json.loads(await ws.receive_text())

				assert resp["type"] == "echo"
				assert resp["data"]["received_type"] == "status_query"

	async def test_ws_message_routing_mcp(self, app):
		try:
			from httpx_ws import aconnect_ws
			from httpx_ws.transport import ASGIWebSocketTransport
		except ImportError:
			pytest.skip("httpx_ws not installed")

		async with AsyncClient(
			transport=ASGIWebSocketTransport(app=app),
			base_url="http://test",
		) as ws_client:
			async with aconnect_ws("/ws/test-conv-mcp", ws_client) as ws:
				await ws.send_json({"api_key": API_KEY, "jwt_token": _make_jwt()})
				auth = json.loads(await ws.receive_text())
				assert auth["type"] == "auth_success"

				# Send MCP (JSON-RPC) message
				await ws.send_json({"jsonrpc": "2.0", "method": "tools/list", "id": 1})

				resp = json.loads(await ws.receive_text())
				while resp.get("type") == "ping":
					resp = json.loads(await ws.receive_text())

				assert resp["type"] == "mcp_response"
				assert resp["data"]["jsonrpc"] == "2.0"

	async def test_ws_invalid_json(self, app):
		try:
			from httpx_ws import aconnect_ws
			from httpx_ws.transport import ASGIWebSocketTransport
		except ImportError:
			pytest.skip("httpx_ws not installed")

		async with AsyncClient(
			transport=ASGIWebSocketTransport(app=app),
			base_url="http://test",
		) as ws_client:
			async with aconnect_ws("/ws/test-conv-badjson", ws_client) as ws:
				await ws.send_json({"api_key": API_KEY, "jwt_token": _make_jwt()})
				auth = json.loads(await ws.receive_text())
				assert auth["type"] == "auth_success"

				# Send invalid JSON
				await ws.send_text("not valid json {{{")
				resp = json.loads(await ws.receive_text())
				while resp.get("type") == "ping":
					resp = json.loads(await ws.receive_text())

				assert resp["type"] == "error"
				assert resp["data"]["code"] == "INVALID_JSON"
