"""Lock in the TD-M3 contextvars wiring on the WS endpoint.

The handshake binds ``site_id`` / ``user`` / ``conversation_id`` into a
structlog contextvars frame so every log line emitted on this asyncio
task carries the connection identity. The frame is cleared in
``finally`` so a subsequent task on the same loop doesn't inherit
stale fields.

These tests catch the regression class "someone rebuilt
websocket/connection.py and dropped the binder call." We saw exactly
that during the master merge — the binder was defined in
``alfred.obs.logging_setup`` but never called in production.
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

# Same key/site/user fixtures as test_ws_contract.py — keeps the two
# files easy to read side by side without forcing a shared conftest.
API_KEY = "test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4"
SITE_ID = "test.frappe.cloud"
USER = "admin@test.com"


def _make_jwt(secret: str = API_KEY, **overrides) -> str:
	now = int(time.time())
	payload = {
		"user": USER, "roles": ["System Manager"],
		"site_id": SITE_ID, "iat": now, "exp": now + 3600,
	}
	payload.update(overrides)
	return jwt.encode(payload, secret, algorithm="HS256")


def _handshake_then_disconnect(ws: MagicMock, payload: dict) -> None:
	"""First call returns the handshake; subsequent calls raise as if
	the client closed the socket immediately. That keeps the endpoint's
	read loop short and predictable."""
	calls = {"n": 0}

	async def _recv():
		calls["n"] += 1
		if calls["n"] == 1:
			return json.dumps(payload)
		raise WebSocketDisconnect()

	ws.receive_text = _recv


def _make_ws() -> MagicMock:
	ws = MagicMock()
	ws.accept = AsyncMock()
	ws.send_json = AsyncMock()
	ws.close = AsyncMock()
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.settings = MagicMock()
	ws.app.state.settings.API_SECRET_KEY = API_KEY
	# These need explicit values: connection.py now resolves JWT key
	# via resolve_jwt_signing_key(settings) and passes settings.JWT_ISSUER /
	# JWT_AUDIENCE through to verify_jwt_token. A bare MagicMock returns
	# a MagicMock (truthy) for missing attrs, which crashes PyJWT.
	ws.app.state.settings.JWT_SIGNING_KEY = ""   # legacy fallback to API key
	ws.app.state.settings.JWT_ISSUER = ""
	ws.app.state.settings.JWT_AUDIENCE = ""
	ws.app.state.settings.WS_HEARTBEAT_INTERVAL = 30
	ws.app.state.redis = None
	return ws


@pytest.mark.asyncio
async def test_websocket_endpoint_binds_request_context_after_auth():
	"""Once the handshake authenticates, bind_request_context must be
	called with the connection's site_id / user / conversation_id.

	If this test fails, every subsequent ``logger.info`` line for the
	connection will be missing tenant identity in production logs —
	silent observability regression."""
	ws = _make_ws()
	_handshake_then_disconnect(ws, {
		"api_key": API_KEY,
		"jwt_token": _make_jwt(),
		"site_config": {},
	})

	with patch.object(connection_mod, "bind_request_context") as bind, \
			patch.object(connection_mod, "clear_request_context") as clear:
		await websocket_endpoint(ws, conversation_id="conv-bind")

	bind.assert_called_once_with(
		site_id=SITE_ID, user=USER, conversation_id="conv-bind",
	)
	# clear MUST run in the endpoint's finally even though the only
	# work after auth was a clean WebSocketDisconnect.
	clear.assert_called_once()


@pytest.mark.asyncio
async def test_websocket_endpoint_skips_binding_on_auth_failure():
	"""Failed auth → no conn → no fields to bind. The wiring must
	not call the binder with ``None`` args, otherwise an attacker
	could pollute the log frame with empty identity fields and the
	subsequent legit connection on the same loop would inherit them."""
	ws = _make_ws()
	# Wrong API key → handshake closes before returning a conn.
	_handshake_then_disconnect(ws, {
		"api_key": "wrong-key-32bytes-padding-padding",
		"jwt_token": _make_jwt(),
		"site_config": {},
	})

	with patch.object(connection_mod, "bind_request_context") as bind, \
			patch.object(connection_mod, "clear_request_context") as clear:
		await websocket_endpoint(ws, conversation_id="conv-noauth")

	bind.assert_not_called()
	clear.assert_not_called()
