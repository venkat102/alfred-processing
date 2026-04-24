"""Connection-state plumbing for the WebSocket handler (TD-H2 split from
``alfred/api/websocket.py``).

This module owns:
  - ``ConnectionState`` — per-connection memory for an authenticated
    session (site_id, user, MCP client, pending questions, pipeline task).
  - ``ws_router`` — the FastAPI ``APIRouter`` instance the app mounts.
  - ``_authenticate_handshake`` — one-shot auth + MCP-client wiring.
  - ``_classify_message`` / ``_handle_mcp_message`` / ``_handle_custom_message``
    — per-frame dispatch.
  - ``_run_agent_pipeline`` / ``_heartbeat_loop`` / ``websocket_endpoint``
    — the top-level connection lifecycle.

The dry-run, clarify, and rescue stages of the pipeline live in
``alfred.api.websocket.pipeline_stages`` to keep file sizes under the
TD-H2 800-LOC target.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from alfred.middleware.auth import verify_jwt_token
from alfred.middleware.rate_limit import check_rate_limit
from alfred.api.websocket.extract import _describe_tool_call

# Server-side rate limit for WebSocket prompts. Matches the REST path
# (alfred.api.routes.SERVER_DEFAULT_RATE_LIMIT). Tests patch this
# constant directly; never read it from site_config (which is
# client-supplied and therefore spoofable).
SERVER_DEFAULT_RATE_LIMIT = 20

if TYPE_CHECKING:
	from alfred.agents.crew import CrewState
	from alfred.api.pipeline import PipelineContext
	from alfred.tools.mcp_client import MCPClient

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
		# MCP client: lets agents fetch live site data via the Client App's MCP server.
		# Initialized after handshake in _authenticate_handshake.
		self.mcp_client: "MCPClient | None" = None
		# Active pipeline task for this conversation. Used to reject concurrent
		# prompts and to cancel in-flight work when the WebSocket closes.
		self.active_pipeline: asyncio.Task | None = None
		# Context for the currently-running pipeline. Exposed so a user-initiated
		# "cancel" message can flip should_stop without tearing down the connection.
		self.active_pipeline_ctx: "PipelineContext | None" = None

	async def send(self, message: dict):
		"""Send a message over the WebSocket.

		Raises WebSocketDisconnect (or similar) if the socket is closed. Callers
		that want to tolerate a dropped connection mid-pipeline (e.g., the
		`on_call` activity-stream callback) should wrap this in try/except -
		we don't swallow here because top-level pipeline steps need to know
		when delivery failed so they can exit cleanly.
		"""
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

		loop = asyncio.get_running_loop()
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
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason="Handshake timeout")
		except (RuntimeError, WebSocketDisconnect, OSError):
			# starlette raises RuntimeError on close-after-close;
			# WebSocketDisconnect if the client already went away;
			# OSError on socket-level failure. None of those should
			# propagate out of the timeout-handler.
			pass
		return None
	except WebSocketDisconnect:
		logger.debug("Client disconnected before handshake: conversation=%s", conversation_id)
		return None
	except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
		# Handshake is a single JSON frame. JSONDecodeError = malformed
		# JSON body. UnicodeDecodeError = non-UTF8 bytes. ValueError is
		# the JSONDecodeError base class (defensive). TimeoutError and
		# WebSocketDisconnect are already handled above.
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason=f"Invalid handshake: {e}")
		except (RuntimeError, WebSocketDisconnect, OSError):
			# Same close-attempt failure modes as the timeout path above.
			pass
		return None

	api_key = handshake.get("api_key", "")
	expected_key = websocket.app.state.settings.API_SECRET_KEY
	# Constant-time comparison - `!=` leaks key bytes via response-latency
	# timing. See alfred/middleware/auth.py::verify_api_key for the REST
	# counterpart and rationale.
	if not hmac.compare_digest(
		api_key.encode("utf-8"), expected_key.encode("utf-8"),
	):
		logger.warning("WS auth failed: invalid API key for conversation=%s", conversation_id)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason="Invalid API key")
		return None

	jwt_token = handshake.get("jwt_token", "")
	# TD-C2: prefer JWT_SIGNING_KEY when set; fall back to API_SECRET_KEY
	# for backward-compat. Startup logs a deprecation warning when the
	# fallback is active (see alfred/main.py::create_app). Splitting the
	# two means compromising the REST bearer key cannot forge JWTs.
	# TD-M1: enforce iss/aud when configured (empty = no enforcement,
	# backward-compat for pre-TD-M1 tokens).
	settings = websocket.app.state.settings
	signing_key = settings.JWT_SIGNING_KEY or settings.API_SECRET_KEY
	try:
		jwt_payload = verify_jwt_token(
			jwt_token,
			signing_key,
			issuer=settings.JWT_ISSUER or None,
			audience=settings.JWT_AUDIENCE or None,
		)
	except ValueError as e:
		logger.warning("WS auth failed: JWT error for conversation=%s: %s", conversation_id, e)
		await websocket.close(code=WS_CLOSE_AUTH_FAILED, reason=str(e))
		return None

	site_config = handshake.get("site_config", {})

	conn = ConnectionState(
		websocket=websocket,
		site_id=jwt_payload["site_id"],
		user=jwt_payload["user"],
		roles=jwt_payload["roles"],
		site_config=site_config,
	)

	# Wire the MCP client so agents can call live Client App tools.
	# The client is bound to this event loop so handle_response from the WS
	# listener safely resolves futures on the correct loop.
	from alfred.tools.mcp_client import MCPClient

	async def _on_tool_call(tool_name: str, arguments: dict):
		"""Stream each tool call as an activity event so the UI shows concrete
		progress ('Reading Leave Application schema...') instead of a silent
		pipeline. Runs on the main loop via await conn.send()."""
		await conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_activity",
			"data": {
				"tool": tool_name,
				"description": _describe_tool_call(tool_name, arguments),
			},
		})

	conn.mcp_client = MCPClient(
		send_func=conn.send,
		main_loop=asyncio.get_running_loop(),
		timeout=int(site_config.get("mcp_timeout", 30)),
		on_call=_on_tool_call,
	)

	return conn


def _classify_message(data: dict) -> str:
	if "jsonrpc" in data:
		return "mcp"
	return "custom"


async def _handle_mcp_message(data: dict, websocket: WebSocket, conn: ConnectionState):
	"""Route an incoming JSON-RPC message.

	The Processing App *sends* tool-call requests to the Client App and *receives*
	responses back on the same WebSocket. So any JSON-RPC message arriving here
	should be a response - we forward it to the MCP client's future resolver.

	If a request arrives (has "method", no "result"), it's unexpected: the Client
	App doesn't currently initiate MCP requests toward the Processing App. Log and
	drop.
	"""
	if "result" in data or "error" in data:
		if conn.mcp_client is None:
			logger.warning(
				"MCP response received but no mcp_client on connection %s: %s",
				conn.site_id, data.get("id"),
			)
			return
		conn.mcp_client.handle_response(data)
		return

	logger.warning(
		"Unexpected MCP request from client %s@%s: method=%s",
		conn.user, conn.site_id, data.get("method"),
	)


async def _handle_custom_message(data: dict, websocket: WebSocket, conn: ConnectionState, conversation_id: str):
	"""Handle a custom protocol message - route by type."""
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

	if msg_type == "cancel":
		# Graceful cancel: flip should_stop so the pipeline exits at the next
		# phase boundary via the existing _send_error path. Leaves the WS open
		# so the user can keep chatting in the same conversation.
		ctx = conn.active_pipeline_ctx
		if ctx is None or conn.active_pipeline is None or conn.active_pipeline.done():
			logger.info(
				"Cancel requested for %s@%s but no pipeline is running",
				conn.user, conn.site_id,
			)
			return
		logger.info(
			"User-initiated cancel for %s@%s conversation=%s",
			conn.user, conn.site_id, conversation_id,
		)
		ctx.stop("Cancelled by user", code="user_cancel")
		return

	if msg_type == "prompt":
		# Core pipeline: user sent a prompt - run the agent crew
		prompt_text = data.get("data", {}).get("text", "")
		manual_mode = (data.get("data", {}).get("mode") or "auto").strip().lower()
		if manual_mode not in ("auto", "dev", "plan", "insights"):
			manual_mode = "auto"
		force_dev = bool(data.get("data", {}).get("force_dev", False))
		if not prompt_text:
			return

		# TD-M6: reject new work during graceful shutdown so in-flight
		# pipelines can drain. Clients should retry after reconnecting
		# to a healthy replica.
		if getattr(websocket.app.state, "shutting_down", False):
			await websocket.send_json({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {
					"error": "Server is shutting down; retry on a fresh connection.",
					"code": "SHUTTING_DOWN",
				},
			})
			return

		# If the pipeline is paused waiting for a clarification answer, route
		# this prompt to the oldest pending question instead of starting a new
		# pipeline. This lets the user just type their answer in the chat box
		# without any special UI state - the frontend stays simple. Routed
		# answers are NOT rate-limited: the pipeline is already running and
		# we consumed a slot when we spawned it.
		if conn._pending_questions:
			oldest_id = next(iter(conn._pending_questions))
			logger.info(
				"Routing prompt as answer to pending question %s for %s@%s",
				oldest_id, conn.user, conn.site_id,
			)
			conn.resolve_question(oldest_id, prompt_text)
			return

		# Per-user rate limit. Placed BEFORE the concurrency check so a
		# user flooding N parallel prompts burns their quota even on the
		# prompts that the concurrency guard would have rejected - good
		# anti-abuse property. Server-side constant; the client's
		# site_config cannot raise its own quota.
		redis = websocket.app.state.redis
		allowed, remaining, retry_after = await check_rate_limit(
			redis, conn.site_id, conn.user,
			SERVER_DEFAULT_RATE_LIMIT, source="websocket",
		)
		if not allowed:
			logger.warning(
				"WS prompt rate-limited for %s@%s (retry_after=%ds)",
				conn.user, conn.site_id, retry_after,
			)
			await websocket.send_json({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {
					"error": f"Rate limit exceeded. Retry after {retry_after} seconds.",
					"code": "RATE_LIMITED",
					"retry_after": retry_after,
					"remaining": remaining,
				},
			})
			return

		# Reject concurrent prompts on the same conversation. Two parallel
		# pipelines would race on CrewState in Redis, produce conflicting
		# Alfred Changeset rows, and interleave WS events at the UI.
		if conn.active_pipeline and not conn.active_pipeline.done():
			logger.warning(
				"Rejecting prompt: pipeline already running for %s@%s",
				conn.user, conn.site_id,
			)
			await websocket.send_json({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {
					"error": "A pipeline is already running for this conversation. Wait for it to finish before sending another prompt.",
					"code": "PIPELINE_BUSY",
				},
			})
			return

		async def _run_and_clear():
			# TD-M6: track in-flight pipelines so graceful shutdown
			# can wait for them to drain. Guarded getattr so tests
			# with a stubbed app.state still work.
			app_state = getattr(websocket.app, "state", None)
			if app_state is not None:
				app_state.active_pipelines = getattr(app_state, "active_pipelines", 0) + 1
			try:
				await _run_agent_pipeline(
					conn, conversation_id, prompt_text,
					manual_mode=manual_mode, force_dev=force_dev,
				)
			finally:
				conn.active_pipeline = None
				if app_state is not None:
					app_state.active_pipelines = max(
						0, getattr(app_state, "active_pipelines", 1) - 1,
					)

		conn.active_pipeline = asyncio.create_task(_run_and_clear())
		return

	logger.info("Custom message from %s@%s: type=%s", conn.user, conn.site_id, msg_type)

	# Unknown type - echo back
	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "echo",
		"data": {"received_type": msg_type, "received_msg_id": msg_id},
	})

async def _run_agent_pipeline(
	conn: ConnectionState,
	conversation_id: str,
	prompt: str,
	manual_mode: str = "auto",
	force_dev: bool = False,
):
	"""Run the full agent SDLC pipeline for a user prompt.

	Thin wrapper over `AgentPipeline` (Phase 3 #12 state machine). The
	orchestrator handles phase sequencing, tracer spans, and error boundaries.
	Adding a new phase: edit `alfred/api/pipeline.py`, not here.

	Args:
		manual_mode: The user's chat-mode selection from the UI switcher.
			One of "auto" | "dev" | "plan" | "insights". The orchestrator
			phase decides the final mode from this + prompt + memory.
		force_dev: When True, bypass the analytics-shape redirect in
			``classify_mode``. Sent by the frontend when the user clicks
			"Run in Dev anyway" on the redirect banner.
	"""
	from alfred.api.pipeline import AgentPipeline, PipelineContext

	ctx = PipelineContext(
		conn=conn,
		conversation_id=conversation_id,
		prompt=prompt,
		manual_mode_override=manual_mode,
		force_dev_override=force_dev,
	)
	conn.active_pipeline_ctx = ctx
	try:
		await AgentPipeline(ctx).run()
	finally:
		conn.active_pipeline_ctx = None


async def _heartbeat_loop(websocket: WebSocket, interval: int = 30):
	try:
		while True:
			await asyncio.sleep(interval)
			await websocket.send_json({"msg_id": str(uuid.uuid4()), "type": "ping", "data": {}})
	except Exception:  # noqa: BLE001 — heartbeat is pure best-effort; any failure (send on closed socket, cancellation, RuntimeError) just ends the loop. Outer handler cancels the task on disconnect anyway.
		pass


@ws_router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
	"""WebSocket endpoint - authenticates, then routes messages and runs agent pipeline."""
	await websocket.accept()
	logger.info("WebSocket connection opened: conversation=%s", conversation_id)

	conn = await _authenticate_handshake(websocket, conversation_id)
	if conn is None:
		return

	# TD-M3: bind structured context for the life of this connection so
	# every log line from the pipeline / handlers / tools carries
	# site_id, user, conversation_id without the caller having to pass
	# them explicitly.
	from alfred.obs.logging_setup import bind_request_context, clear_request_context
	bind_request_context(
		site_id=conn.site_id,
		user=conn.user,
		conversation_id=conversation_id,
	)

	logger.info("WebSocket authenticated: user=%s, site=%s, conversation=%s", conn.user, conn.site_id, conversation_id)

	# Register connection
	_connections[conversation_id] = conn

	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "auth_success",
		"data": {"user": conn.user, "site_id": conn.site_id, "conversation_id": conversation_id},
	})

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
	except Exception as e:  # noqa: BLE001 — top-level WebSocket endpoint; any unhandled exception that bubbles up to uvicorn would kill the connection without logging. Log and let the finally clean up state.
		logger.error("WebSocket error: user=%s, conversation=%s, error=%s", conn.user, conversation_id, e)
	finally:
		_connections.pop(conversation_id, None)
		heartbeat_task.cancel()
		try:
			await heartbeat_task
		except asyncio.CancelledError:
			pass

		# Cancel any in-flight pipeline so orphaned crews don't keep burning
		# LLM calls after the user has already disconnected.
		if conn.active_pipeline and not conn.active_pipeline.done():
			logger.info("Cancelling in-flight pipeline for %s", conversation_id)
			conn.active_pipeline.cancel()
			try:
				await conn.active_pipeline
			except (asyncio.CancelledError, Exception):  # noqa: BLE001 — intentional double-catch: CancelledError is expected after .cancel(); any other Exception from the pipeline's own error handling must not block disconnect cleanup. CancelledError is explicit because it's a BaseException subclass in Py 3.8+ and wouldn't be caught by Exception alone.
				pass

		# TD-M3: drop the bound structured-log context so a later
		# connection on the same asyncio task doesn't inherit stale
		# site_id / user / conversation_id.
		clear_request_context()
