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

from alfred.middleware.auth import verify_jwt_token
from alfred.state.store import StateStore

logger = logging.getLogger("alfred.websocket")

ws_router = APIRouter()

# WebSocket close codes
WS_CLOSE_AUTH_FAILED = 4001
WS_CLOSE_RATE_LIMIT = 4002
WS_CLOSE_INVALID_HANDSHAKE = 4003
WS_CLOSE_HEARTBEAT_TIMEOUT = 4004

# Active connections: conversation_id -> ConnectionState
_connections: dict[str, "ConnectionState"] = {}


class ConnectionState:
	"""Per-connection state for an authenticated WebSocket session."""

	def __init__(self, websocket: WebSocket, site_id: str, user: str, roles: list[str], site_config: dict):
		self.websocket = websocket
		self.site_id = site_id
		self.user = user
		self.roles = roles
		self.site_config = site_config
		self.last_acked_msg_id: str | None = None
		self.pending_acks: dict[str, dict] = {}
		# For human_input: map of question msg_id -> asyncio.Future
		self._pending_questions: dict[str, asyncio.Future] = {}

	async def send(self, message: dict):
		"""Send a message over the WebSocket."""
		await self.websocket.send_json(message)

	async def ask_human(self, question: str, choices: list[str] | None = None, timeout: int = 900) -> str:
		"""Send a question to the user and wait for their response.

		This is called by the CrewAI human_input override.
		Sends the question via WebSocket and blocks until the user responds.
		"""
		msg_id = str(uuid.uuid4())
		message = {
			"msg_id": msg_id,
			"type": "question",
			"data": {"question": question, "choices": choices or [], "timeout_seconds": timeout},
		}

		loop = asyncio.get_event_loop()
		future = loop.create_future()
		self._pending_questions[msg_id] = future

		try:
			await self.send(message)
			response = await asyncio.wait_for(future, timeout=timeout)
			return response
		except asyncio.TimeoutError:
			return "[TIMEOUT] User did not respond. Consider escalating to a human operator."
		finally:
			self._pending_questions.pop(msg_id, None)

	def resolve_question(self, msg_id: str, answer: str):
		"""Resolve a pending question with the user's answer."""
		future = self._pending_questions.get(msg_id)
		if future and not future.done():
			future.set_result(answer)


async def _authenticate_handshake(
	websocket: WebSocket, conversation_id: str
) -> ConnectionState | None:
	"""Wait for and validate the handshake message."""
	try:
		raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
		handshake = json.loads(raw)
	except asyncio.TimeoutError:
		await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason="Handshake timeout")
		return None
	except (json.JSONDecodeError, Exception) as e:
		await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason=f"Invalid handshake: {e}")
		return None

	api_key = handshake.get("api_key", "")
	expected_key = websocket.app.state.settings.API_SECRET_KEY
	if api_key != expected_key:
		logger.warning("WS auth failed: invalid API key for conversation=%s", conversation_id)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason="Invalid API key")
		return None

	jwt_token = handshake.get("jwt_token", "")
	try:
		jwt_payload = verify_jwt_token(jwt_token, expected_key)
	except ValueError as e:
		logger.warning("WS auth failed: JWT error for conversation=%s: %s", conversation_id, e)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason=str(e))
		return None

	site_config = handshake.get("site_config", {})

	return ConnectionState(
		websocket=websocket,
		site_id=jwt_payload["site_id"],
		user=jwt_payload["user"],
		roles=jwt_payload["roles"],
		site_config=site_config,
	)


def _classify_message(data: dict) -> str:
	if "jsonrpc" in data:
		return "mcp"
	return "custom"


async def _handle_mcp_message(data: dict, websocket: WebSocket, conn: ConnectionState):
	"""Handle an MCP (JSON-RPC) protocol message — forward to MCP client."""
	logger.debug("MCP message from %s@%s: method=%s", conn.user, conn.site_id, data.get("method"))
	response = {
		"msg_id": str(uuid.uuid4()),
		"type": "mcp_response",
		"data": {
			"jsonrpc": "2.0",
			"id": data.get("id"),
			"result": {"status": "mcp_forwarding_not_implemented"},
		},
	}
	await websocket.send_json(response)


async def _handle_custom_message(data: dict, websocket: WebSocket, conn: ConnectionState, conversation_id: str):
	"""Handle a custom protocol message — route by type."""
	msg_type = data.get("type", "unknown")
	msg_id = data.get("msg_id", "")

	if msg_type == "ack":
		acked_id = data.get("data", {}).get("msg_id", msg_id)
		conn.pending_acks.pop(acked_id, None)
		conn.last_acked_msg_id = acked_id
		return

	if msg_type == "resume":
		return

	if msg_type == "user_response":
		# User responding to a question from an agent
		response_to = data.get("data", {}).get("response_to", msg_id)
		answer = data.get("data", {}).get("text", "")
		conn.resolve_question(response_to, answer)
		return

	if msg_type == "prompt":
		# Core pipeline: user sent a prompt — run the agent crew
		prompt_text = data.get("data", {}).get("text", "")
		if prompt_text:
			asyncio.create_task(
				_run_agent_pipeline(conn, conversation_id, prompt_text)
			)
			return

	logger.info("Custom message from %s@%s: type=%s", conn.user, conn.site_id, msg_type)

	# Unknown type — echo back
	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "echo",
		"data": {"received_type": msg_type, "received_msg_id": msg_id},
	})


async def _run_agent_pipeline(conn: ConnectionState, conversation_id: str, prompt: str):
	"""Run the full agent SDLC pipeline for a user prompt.

	This is the core integration point — it connects the WebSocket
	to the CrewAI crew and streams events back to the user.
	"""
	from alfred.defense.sanitizer import check_prompt

	# Step 1: Prompt defense
	defense_result = check_prompt(prompt)
	if not defense_result["allowed"]:
		await conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "error",
			"data": {
				"error": defense_result["rejection_reason"],
				"code": "PROMPT_BLOCKED" if not defense_result["needs_review"] else "NEEDS_REVIEW",
			},
		})
		return

	# Step 2: Plan check (admin portal)
	redis = getattr(conn.websocket.app.state, "redis", None)
	store = StateStore(redis) if redis else None

	settings = conn.websocket.app.state.settings
	admin_url = getattr(settings, "ADMIN_PORTAL_URL", "")
	admin_key = getattr(settings, "ADMIN_SERVICE_KEY", "")
	if admin_url and admin_key:
		try:
			from alfred.api.admin_client import AdminClient
			admin = AdminClient(admin_url, admin_key, redis)
			plan_result = await admin.check_plan(conn.site_id)
			if not plan_result.get("allowed", True):
				await conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "error",
					"data": {
						"error": plan_result.get("reason", "Plan limit exceeded"),
						"code": "PLAN_EXCEEDED",
						"warning": plan_result.get("warning"),
					},
				})
				return
			# Send warning if approaching limit
			if plan_result.get("warning"):
				await conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {"agent": "System", "status": "warning", "message": plan_result["warning"]},
				})
		except Exception as e:
			logger.warning("Plan check failed (allowing by default): %s", e)

	# Step 3: Notify user that processing has started
	await conn.send({
		"msg_id": str(uuid.uuid4()),
		"type": "agent_status",
		"data": {"agent": "Orchestrator", "status": "started", "phase": "requirement"},
	})

	# Step 4: Build and run the crew
	try:
		from alfred.agents.crew import build_alfred_crew, run_crew, load_crew_state

		# Check for existing state (resumption)
		previous_state = None
		if store:
			previous_state = await load_crew_state(store, conn.site_id, conversation_id)

		user_context = {
			"user": conn.user,
			"roles": conn.roles,
			"site_id": conn.site_id,
		}

		crew, state = build_alfred_crew(
			user_prompt=prompt,
			user_context=user_context,
			site_config=conn.site_config,
			previous_state=previous_state,
		)

		# Event callback — streams agent events to the user via WebSocket
		async def event_callback(event_type: str, data: dict):
			await conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "agent_status",
				"data": {"event": event_type, **data},
			})

		# Run with timeout
		timeout = conn.site_config.get("task_timeout_seconds", 300)
		result = await asyncio.wait_for(
			run_crew(crew, state, store, conn.site_id, conversation_id, event_callback),
			timeout=timeout * 6,  # Total pipeline timeout = per-task * 6 phases
		)

		# Step 5: Send result back
		if result["status"] == "completed":
			await conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "agent_status",
				"data": {"agent": "Orchestrator", "status": "completed", "result": result.get("result", "")[:2000]},
			})
		else:
			await conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {"error": result.get("error", "Pipeline failed"), "code": "PIPELINE_FAILED"},
			})

	except asyncio.TimeoutError:
		logger.error("Pipeline timeout for conversation=%s", conversation_id)
		await conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "error",
			"data": {"error": "Pipeline timed out. The conversation has been saved — you can resume later.", "code": "PIPELINE_TIMEOUT"},
		})
	except Exception as e:
		logger.error("Pipeline error for conversation=%s: %s", conversation_id, e, exc_info=True)
		await conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "error",
			"data": {"error": str(e), "code": "PIPELINE_ERROR"},
		})


async def _heartbeat_loop(websocket: WebSocket, interval: int = 30):
	try:
		while True:
			await asyncio.sleep(interval)
			await websocket.send_json({"msg_id": str(uuid.uuid4()), "type": "ping", "data": {}})
	except Exception:
		pass


@ws_router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
	"""WebSocket endpoint — authenticates, then routes messages and runs agent pipeline."""
	await websocket.accept()
	logger.info("WebSocket connection opened: conversation=%s", conversation_id)

	conn = await _authenticate_handshake(websocket, conversation_id)
	if conn is None:
		return

	logger.info("WebSocket authenticated: user=%s, site=%s, conversation=%s", conn.user, conn.site_id, conversation_id)

	# Register connection
	_connections[conversation_id] = conn

	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "auth_success",
		"data": {"user": conn.user, "site_id": conn.site_id, "conversation_id": conversation_id},
	})

	redis = getattr(websocket.app.state, "redis", None)
	store = StateStore(redis) if redis else None

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

			msg_class = _classify_message(data)
			if msg_class == "mcp":
				await _handle_mcp_message(data, websocket, conn)
			else:
				await _handle_custom_message(data, websocket, conn, conversation_id)

	except WebSocketDisconnect:
		logger.info("WebSocket disconnected: user=%s, conversation=%s", conn.user, conversation_id)
	except Exception as e:
		logger.error("WebSocket error: user=%s, conversation=%s, error=%s", conn.user, conversation_id, e)
	finally:
		_connections.pop(conversation_id, None)
		heartbeat_task.cancel()
		try:
			await heartbeat_task
		except asyncio.CancelledError:
			pass
