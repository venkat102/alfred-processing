"""Regression test for the audit's P1.2 — WS reconnect orphan.

Before the fix, ``websocket_endpoint`` did:

    _connections[conversation_id] = conn

…with no eviction of any prior entry under the same id. A client
reconnecting (page reload, mobile network blip) left the previous
``ConnectionState``'s ``active_pipeline`` task running with its own
MCP futures bound to the old loop — a zombie crew burning LLM tokens
until the per-task timeout. Worse, both pipelines wrote events to
the same Redis stream, confusing replay clients.

The fix evicts the prior entry before installing the new conn:
  - cancels the old ``active_pipeline`` task (if running),
  - closes the old socket with ``WS_CLOSE_SUPERSEDED`` (4005),
  - then installs the new conn under the same key.

These tests pin the contract.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from fastapi import WebSocketDisconnect

import alfred.api.websocket.connection as connection_mod
from alfred.api.websocket import (
	WS_CLOSE_SUPERSEDED,
	ConnectionState,
	websocket_endpoint,
)

API_KEY = "test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4"
SITE_ID = "site-a"
USER = "alice@example.com"


def _mint_jwt() -> str:
	now = int(time.time())
	return jwt.encode(
		{
			"user": USER, "roles": ["System Manager"],
			"site_id": SITE_ID, "iat": now, "exp": now + 3600,
		},
		API_KEY, algorithm="HS256",
	)


def _make_ws() -> MagicMock:
	ws = MagicMock()
	ws.accept = AsyncMock()
	ws.send_json = AsyncMock()
	ws.close = AsyncMock()
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.settings = MagicMock()
	ws.app.state.settings.API_SECRET_KEY = API_KEY
	ws.app.state.settings.WS_HEARTBEAT_INTERVAL = 30
	ws.app.state.redis = None
	ws.app.state.shutting_down = False
	ws.app.state.active_pipelines = 0
	return ws


def _handshake_then_disconnect(ws: MagicMock) -> None:
	calls = {"n": 0}

	async def _recv():
		calls["n"] += 1
		if calls["n"] == 1:
			return json.dumps({
				"api_key": API_KEY,
				"jwt_token": _mint_jwt(),
				"site_config": {},
			})
		raise WebSocketDisconnect()

	ws.receive_text = _recv


@pytest.fixture(autouse=True)
def _reset_connections():
	"""Clear the module-level _connections dict around each test so
	one test's left-overs don't leak to the next."""
	connection_mod._connections.clear()
	yield
	connection_mod._connections.clear()


@pytest.mark.asyncio
async def test_reconnect_with_same_conversation_id_evicts_prior_conn():
	"""Two consecutive connections with the same conversation_id: the
	second one boots out the first. Without this, the first conn's
	active_pipeline kept burning LLM tokens with no observer."""

	# Stage a "prior" conn manually to avoid having to spin up two real
	# endpoints in sequence — the eviction logic is what matters here.
	old_ws = _make_ws()
	old_pipeline = MagicMock()
	old_pipeline.done.return_value = False
	old_pipeline.cancel = MagicMock()
	old_conn = ConnectionState(
		websocket=old_ws, site_id=SITE_ID, user=USER,
		roles=["System Manager"], site_config={},
		conversation_id="conv-1",
	)
	old_conn.active_pipeline = old_pipeline
	connection_mod._connections["conv-1"] = old_conn

	# Now drive a fresh connection through the real endpoint.
	new_ws = _make_ws()
	_handshake_then_disconnect(new_ws)

	await websocket_endpoint(new_ws, conversation_id="conv-1")

	# The old pipeline must have been cancelled, the old socket closed
	# with the documented supersede code.
	old_pipeline.cancel.assert_called_once()
	old_ws.close.assert_awaited_once()
	close_kwargs = old_ws.close.await_args.kwargs
	assert close_kwargs["code"] == WS_CLOSE_SUPERSEDED


@pytest.mark.asyncio
async def test_reconnect_does_not_cancel_already_done_pipeline():
	"""The cancel() should only fire when the prior pipeline is still
	running. A finished task doesn't need cancellation, and calling
	cancel() on a done task is a no-op anyway — but wasting the call
	makes the eviction loop spookier to read."""
	old_ws = _make_ws()
	completed_pipeline = MagicMock()
	completed_pipeline.done.return_value = True
	completed_pipeline.cancel = MagicMock()
	old_conn = ConnectionState(
		websocket=old_ws, site_id=SITE_ID, user=USER,
		roles=["System Manager"], site_config={},
		conversation_id="conv-2",
	)
	old_conn.active_pipeline = completed_pipeline
	connection_mod._connections["conv-2"] = old_conn

	new_ws = _make_ws()
	_handshake_then_disconnect(new_ws)
	await websocket_endpoint(new_ws, conversation_id="conv-2")

	# Pipeline was already done — no cancel needed.
	completed_pipeline.cancel.assert_not_called()
	# Old socket still gets a supersede close so any half-open
	# connection (rare) cleans up.
	old_ws.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_first_connection_does_not_self_evict():
	"""No prior entry → no eviction work, no spurious close on the new
	socket itself."""
	new_ws = _make_ws()
	_handshake_then_disconnect(new_ws)

	await websocket_endpoint(new_ws, conversation_id="conv-fresh")

	# No supersede close called on the new socket. (close may still be
	# called by other paths like the auth-failed branch — assert
	# specifically that it wasn't called with the supersede code.)
	for call in new_ws.close.await_args_list:
		assert call.kwargs.get("code") != WS_CLOSE_SUPERSEDED


@pytest.mark.asyncio
async def test_old_socket_close_failure_does_not_block_new_connection():
	"""Old socket is almost always already gone by the time a reconnect
	hits — that's why the client reconnected. ``await old_ws.close()``
	will often raise. The eviction must swallow that and proceed with
	the new conn install."""
	old_ws = _make_ws()
	# Simulate "already closed" — close() raises.
	old_ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
	old_pipeline = MagicMock()
	old_pipeline.done.return_value = False
	old_conn = ConnectionState(
		websocket=old_ws, site_id=SITE_ID, user=USER,
		roles=["System Manager"], site_config={},
		conversation_id="conv-already-gone",
	)
	old_conn.active_pipeline = old_pipeline
	connection_mod._connections["conv-already-gone"] = old_conn

	new_ws = _make_ws()
	_handshake_then_disconnect(new_ws)

	# Must not raise.
	await websocket_endpoint(new_ws, conversation_id="conv-already-gone")

	# Pipeline still got cancelled even though the socket close raised.
	old_pipeline.cancel.assert_called_once()
	# New conn IS now installed in _connections — but the endpoint's
	# own finally pops it on disconnect, so we can only assert eviction
	# happened, not that the new entry persists past the test.
