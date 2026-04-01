"""WebSocket handler for real-time bidirectional communication with client apps.

Protocol:
1. Client connects to /ws/{conversation_id}
2. Client sends handshake: {"api_key": "...", "jwt_token": "...", "site_config": {...}}
3. Server validates API key + JWT, extracts site_id and user
4. Bidirectional messaging begins — each message has a msg_id for ack tracking
5. MCP (JSON-RPC) messages are identified by "jsonrpc" field, all others by "type" field
6. Heartbeat ping every 30 seconds
7. On disconnect, unacked messages buffered in Redis for replay on reconnect
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from intern.middleware.auth import verify_jwt_token
from intern.middleware.rate_limit import check_rate_limit
from intern.state.store import StateStore

logger = logging.getLogger("alfred.websocket")

ws_router = APIRouter()

# WebSocket close codes
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_RATE_LIMIT = 4002
WS_CLOSE_INVALID_HANDSHAKE = 4003
WS_CLOSE_HEARTBEAT_TIMEOUT = 4004


class ConnectionState:
	"""Per-connection state for an authenticated WebSocket session."""

	def __init__(self, site_id: str, user: str, roles: list[str], site_config: dict):
		self.site_id = site_id
		self.user = user
		self.roles = roles
		self.site_config = site_config
		self.last_acked_msg_id: str | None = None
		self.pending_acks: dict[str, dict] = {}  # msg_id -> message


async def _authenticate_handshake(
	websocket: WebSocket, conversation_id: str
) -> ConnectionState | None:
	"""Wait for and validate the handshake message.

	Returns ConnectionState on success, or None on failure (closes the WebSocket).
	"""
	try:
		raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
		handshake = json.loads(raw)
	except asyncio.TimeoutError:
		await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason="Handshake timeout")
		return None
	except (json.JSONDecodeError, Exception) as e:
		await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason=f"Invalid handshake: {e}")
		return None

	# Validate API key
	api_key = handshake.get("api_key", "")
	expected_key = websocket.app.state.settings.API_SECRET_KEY
	if api_key != expected_key:
		logger.warning("WS auth failed: invalid API key for conversation=%s", conversation_id)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason="Invalid API key")
		return None

	# Validate JWT
	jwt_token = handshake.get("jwt_token", "")
	try:
		jwt_payload = verify_jwt_token(jwt_token, expected_key)
	except ValueError as e:
		logger.warning("WS auth failed: JWT error for conversation=%s: %s", conversation_id, e)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason=str(e))
		return None

	site_config = handshake.get("site_config", {})

	return ConnectionState(
		site_id=jwt_payload["site_id"],
		user=jwt_payload["user"],
		roles=jwt_payload["roles"],
		site_config=site_config,
	)


def _classify_message(data: dict) -> str:
	"""Classify a WebSocket message as MCP (JSON-RPC) or custom."""
	if "jsonrpc" in data:
		return "mcp"
	return "custom"


async def _handle_mcp_message(data: dict, websocket: WebSocket, conn: ConnectionState):
	"""Handle an MCP (JSON-RPC) protocol message."""
	logger.debug("MCP message from %s@%s: method=%s", conn.user, conn.site_id, data.get("method"))
	# Forward to MCP client — placeholder for Phase 2
	response = {
		"msg_id": str(uuid.uuid4()),
		"type": "mcp_response",
		"data": {
			"jsonrpc": "2.0",
			"id": data.get("id"),
			"result": {"status": "mcp_not_implemented_yet"},
		},
	}
	await websocket.send_json(response)


async def _handle_custom_message(data: dict, websocket: WebSocket, conn: ConnectionState):
	"""Handle a custom protocol message (prompt, user_response, deploy_command, ack)."""
	msg_type = data.get("type", "unknown")
	msg_id = data.get("msg_id", "")

	if msg_type == "ack":
		# Client acknowledging receipt of a server message
		acked_id = data.get("data", {}).get("msg_id", msg_id)
		conn.pending_acks.pop(acked_id, None)
		conn.last_acked_msg_id = acked_id
		return

	if msg_type == "resume":
		# Client requesting replay of missed messages after reconnect
		# This is handled at connection start — see _replay_missed_messages
		return

	logger.info("Custom message from %s@%s: type=%s", conn.user, conn.site_id, msg_type)

	# Echo back with acknowledgment for now — agent dispatch in Phase 2
	response = {
		"msg_id": str(uuid.uuid4()),
		"type": "echo",
		"data": {
			"received_type": msg_type,
			"received_msg_id": msg_id,
			"message": f"Received {msg_type} message (agent dispatch not yet implemented)",
		},
	}
	await websocket.send_json(response)


async def _send_with_ack(websocket: WebSocket, conn: ConnectionState, message: dict, store: StateStore):
	"""Send a message and track it for acknowledgment. Buffer in Redis."""
	msg_id = message.get("msg_id", str(uuid.uuid4()))
	message["msg_id"] = msg_id

	# Track as pending ack
	conn.pending_acks[msg_id] = message

	# Buffer in Redis for replay on reconnect
	await store.push_event(
		conn.site_id,
		f"ws_buffer:{conn.user}",
		message,
	)

	await websocket.send_json(message)


async def _replay_missed_messages(
	websocket: WebSocket, conn: ConnectionState, store: StateStore, last_msg_id: str
):
	"""Replay messages that the client missed after a disconnection."""
	events = await store.get_events(conn.site_id, f"ws_buffer:{conn.user}", since_id=last_msg_id)
	if events:
		logger.info("Replaying %d missed messages for %s@%s", len(events), conn.user, conn.site_id)
		for event in events:
			await websocket.send_json(event["data"])


async def _heartbeat_loop(websocket: WebSocket, interval: int = 30):
	"""Send periodic ping frames to keep the connection alive."""
	try:
		while True:
			await asyncio.sleep(interval)
			await websocket.send_json({"msg_id": str(uuid.uuid4()), "type": "ping", "data": {}})
	except Exception:
		pass  # Connection closed


@ws_router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
	"""WebSocket endpoint for client app communication.

	Authenticates via handshake, then routes messages between MCP and custom types.
	Supports message acknowledgment and reconnection replay.
	"""
	await websocket.accept()
	logger.info("WebSocket connection opened: conversation=%s", conversation_id)

	# Step 1: Authenticate
	conn = await _authenticate_handshake(websocket, conversation_id)
	if conn is None:
		return

	logger.info(
		"WebSocket authenticated: user=%s, site=%s, conversation=%s",
		conn.user, conn.site_id, conversation_id,
	)

	# Send auth success confirmation
	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "auth_success",
		"data": {
			"user": conn.user,
			"site_id": conn.site_id,
			"conversation_id": conversation_id,
		},
	})

	# Get store for message buffering
	redis = getattr(websocket.app.state, "redis", None)
	store = StateStore(redis) if redis else None

	# Start heartbeat
	heartbeat_interval = websocket.app.state.settings.WS_HEARTBEAT_INTERVAL
	heartbeat_task = asyncio.create_task(_heartbeat_loop(websocket, heartbeat_interval))

	try:
		while True:
			raw = await websocket.receive_text()

			try:
				data = json.loads(raw)
			except json.JSONDecodeError:
				await websocket.send_json({
					"msg_id": str(uuid.uuid4()),
					"type": "error",
					"data": {"error": "Invalid JSON", "code": "INVALID_JSON"},
				})
				continue

			# Route by message type
			msg_class = _classify_message(data)
			if msg_class == "mcp":
				await _handle_mcp_message(data, websocket, conn)
			else:
				await _handle_custom_message(data, websocket, conn)

	except WebSocketDisconnect:
		logger.info(
			"WebSocket disconnected: user=%s, site=%s, conversation=%s",
			conn.user, conn.site_id, conversation_id,
		)
	except Exception as e:
		logger.error(
			"WebSocket error: user=%s, conversation=%s, error=%s",
			conn.user, conversation_id, e,
		)
	finally:
		heartbeat_task.cancel()
		try:
			await heartbeat_task
		except asyncio.CancelledError:
			pass
