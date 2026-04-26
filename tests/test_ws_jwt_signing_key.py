"""Pin the WebSocket handshake's JWT key + iss/aud wiring.

These tests catch a regression class that nothing else covered: the
WS handshake hardcoding ``API_SECRET_KEY`` for JWT verification (which
breaks ``JWT_SIGNING_KEY`` rotation) and silently dropping the
``JWT_ISSUER`` / ``JWT_AUDIENCE`` claims (which makes the documented
TD-M1 cross-instance replay-prevention control inert).

``test_jwt_signing_key.py`` and ``test_jwt_iss_aud.py`` already pin the
``verify_jwt_token`` helper at the unit level. This file pins the
*caller* — i.e. that ``alfred/api/websocket/connection.py`` passes the
right key + the right claim-enforcement flags into the helper.

Pattern mirrors ``test_ws_request_context.py`` (mock the FastAPI
``WebSocket``, drive ``websocket_endpoint`` end-to-end, observe how it
closes on auth failure or proceeds to ``bind_request_context`` on
success) so the two files read side by side.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import WebSocketDisconnect

import alfred.api.websocket.connection as connection_mod
from alfred.api.websocket import websocket_endpoint

API_KEY = "test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4"
JWT_KEY = "test-jwt-signing-key-distinct-from-the-api-key-12345"
SITE_ID = "test.frappe.cloud"
USER = "admin@test.com"


def _make_jwt(secret: str, *, iss: str | None = None, aud: str | None = None) -> str:
	now = int(time.time())
	payload = {
		"user": USER, "roles": ["System Manager"],
		"site_id": SITE_ID, "iat": now, "exp": now + 3600,
	}
	if iss is not None:
		payload["iss"] = iss
	if aud is not None:
		payload["aud"] = aud
	return jwt.encode(payload, secret, algorithm="HS256")


def _handshake_then_disconnect(ws: MagicMock, payload: dict) -> None:
	"""First ``receive_text`` returns the handshake JSON; subsequent
	calls raise as if the client closed. Matches the pattern in
	``test_ws_request_context.py``."""
	calls = {"n": 0}

	async def _recv():
		calls["n"] += 1
		if calls["n"] == 1:
			return json.dumps(payload)
		raise WebSocketDisconnect()

	ws.receive_text = _recv


def _make_ws(
	*,
	api_key: str = API_KEY,
	jwt_signing_key: str = "",
	jwt_issuer: str = "",
	jwt_audience: str = "",
) -> MagicMock:
	ws = MagicMock()
	ws.accept = AsyncMock()
	ws.send_json = AsyncMock()
	ws.close = AsyncMock()
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.settings = MagicMock()
	ws.app.state.settings.API_SECRET_KEY = api_key
	ws.app.state.settings.JWT_SIGNING_KEY = jwt_signing_key
	ws.app.state.settings.JWT_ISSUER = jwt_issuer
	ws.app.state.settings.JWT_AUDIENCE = jwt_audience
	ws.app.state.settings.WS_HEARTBEAT_INTERVAL = 30
	ws.app.state.redis = None
	return ws


# ── JWT signing key resolution (C1 regression guard) ──────────────────


@pytest.mark.asyncio
async def test_ws_accepts_jwt_signed_with_jwt_signing_key_when_set():
	"""When ``JWT_SIGNING_KEY`` is configured, a JWT signed with it must
	be accepted by the WS handshake. This is the substance of the
	TD-C2 key separation: rotating ``JWT_SIGNING_KEY`` should let new
	tokens through on BOTH transports symmetrically.
	"""
	ws = _make_ws(jwt_signing_key=JWT_KEY)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(JWT_KEY),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-key-ok")
	# Auth succeeded → bind_request_context is called once.
	bind.assert_called_once()


@pytest.mark.asyncio
async def test_ws_rejects_jwt_signed_with_api_key_when_jwt_signing_key_set():
	"""The whole point of TD-C2: a leaked ``API_SECRET_KEY`` must not
	be usable to forge a JWT for the WS handshake once the operator
	has rotated ``JWT_SIGNING_KEY`` to a distinct value. An earlier
	version reused ``API_SECRET_KEY`` for JWT verification at this
	call site, defeating the rotation entirely.
	"""
	ws = _make_ws(jwt_signing_key=JWT_KEY)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		# Token signed with the (leaked) API key, not JWT_SIGNING_KEY.
		"jwt_token": _make_jwt(API_KEY),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-stale-key")
	bind.assert_not_called()
	ws.close.assert_called_once()


@pytest.mark.asyncio
async def test_ws_falls_back_to_api_secret_key_when_jwt_signing_key_empty():
	"""Backward-compat: an operator who has not yet configured
	``JWT_SIGNING_KEY`` continues to verify JWTs against
	``API_SECRET_KEY``. test_jwt_signing_key.py logs a startup
	warning to nudge them; this one confirms the runtime fallback
	still authenticates legacy callers.
	"""
	ws = _make_ws(jwt_signing_key="")
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(API_KEY),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-fallback")
	bind.assert_called_once()


# ── Issuer / audience enforcement (C2 regression guard) ──────────────


@pytest.mark.asyncio
async def test_ws_enforces_jwt_issuer_when_configured():
	"""When ``JWT_ISSUER`` is set on settings, the handshake must
	require the token's ``iss`` claim to match. If the WS caller drops
	the issuer kwarg before passing into ``verify_jwt_token`` (as it
	did before this fix), this test fails because a token without
	``iss`` would be silently accepted.
	"""
	ws = _make_ws(
		jwt_signing_key=JWT_KEY,
		jwt_issuer="admin.example.com",
	)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		# Token has no iss claim - must be rejected.
		"jwt_token": _make_jwt(JWT_KEY),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-no-iss")
	bind.assert_not_called()
	ws.close.assert_called_once()


@pytest.mark.asyncio
async def test_ws_rejects_jwt_with_wrong_issuer():
	ws = _make_ws(
		jwt_signing_key=JWT_KEY,
		jwt_issuer="admin.example.com",
	)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(JWT_KEY, iss="attacker.example"),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-bad-iss")
	bind.assert_not_called()
	ws.close.assert_called_once()


@pytest.mark.asyncio
async def test_ws_accepts_jwt_with_matching_issuer_and_audience():
	ws = _make_ws(
		jwt_signing_key=JWT_KEY,
		jwt_issuer="admin.example.com",
		jwt_audience="alfred.prod",
	)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(
			JWT_KEY, iss="admin.example.com", aud="alfred.prod",
		),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-ok-iss-aud")
	bind.assert_called_once()


@pytest.mark.asyncio
async def test_ws_enforces_jwt_audience_when_configured():
	"""Same property as the issuer test, for ``aud``. Catches the
	mirror failure mode where one of the two kwargs got threaded but
	the other was forgotten."""
	ws = _make_ws(
		jwt_signing_key=JWT_KEY,
		jwt_audience="alfred.prod",
	)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(JWT_KEY),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-no-aud")
	bind.assert_not_called()
	ws.close.assert_called_once()


@pytest.mark.asyncio
async def test_ws_rejects_jwt_with_wrong_audience():
	ws = _make_ws(
		jwt_signing_key=JWT_KEY,
		jwt_audience="alfred.prod",
	)
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(JWT_KEY, aud="alfred.staging"),
		"site_config": {},
	})
	with patch.object(connection_mod, "bind_request_context") as bind:
		await websocket_endpoint(ws, conversation_id="conv-bad-aud")
	bind.assert_not_called()
	ws.close.assert_called_once()
