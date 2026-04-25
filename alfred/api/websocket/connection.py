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
import time
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from alfred.middleware.auth import verify_jwt_token
from alfred.obs.tasks import spawn_logged
from alfred.api.websocket.extract import _describe_tool_call

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

# How long a timed-out clarifier question remains eligible for a
# late-response acknowledgement. After this window the msg_id is
# garbage-collected and a later reply just falls through silently
# again - at that point it's almost certainly the user typing in a
# new session, not a genuinely late answer.
_EXPIRED_Q_TTL_SECONDS = 3600

# Active connections: conversation_id -> ConnectionState
_connections: dict[str, "ConnectionState"] = {}


class ConnectionState:
	"""Per-connection state for an authenticated WebSocket session."""

	# Message types we don't persist to the resume stream. Ack, ping, and
	# mcp_response are transport/meta events with no user-facing meaning;
	# echo is a test-only response. Everything else lands in the stream so
	# a reconnecting client can replay what it missed (see `resume`
	# handler in _handle_custom_message).
	_STREAM_SKIP_TYPES: frozenset[str] = frozenset({
		"ack", "ping", "mcp_response", "echo",
	})

	def __init__(
		self,
		websocket: WebSocket,
		site_id: str,
		user: str,
		roles: list[str],
		site_config: dict,
		conversation_id: str | None = None,
		store: "StateStore | None" = None,
	):
		self.websocket = websocket
		self.site_id = site_id
		self.user = user
		self.roles = roles
		self.site_config = site_config
		# conversation_id + store are set at handshake time so ``send``
		# can persist each user-visible message to the Redis stream.
		# They're optional because some tests construct ConnectionState
		# without a handshake - the persist path is a no-op in that case.
		self.conversation_id: str | None = conversation_id
		self.store = store
		self.last_acked_msg_id: str | None = None
		self.pending_acks: dict[str, dict] = {}
		# For human_input: map of question msg_id -> asyncio.Future
		self._pending_questions: dict[str, asyncio.Future] = {}
		# Recently-expired question msg_ids, mapped to the expiry timestamp.
		# When a user's response lands after the Future timed out, the
		# Future + entry in _pending_questions are already gone, so without
		# this side table resolve_question() would silently drop the
		# answer and the user would never know the pipeline proceeded
		# without them. Entries are GC'd after _EXPIRED_Q_TTL_SECONDS.
		self._expired_questions: dict[str, float] = {}
		# MCP client: lets agents fetch live site data via the Client App's MCP server.
		# Initialized after handshake in _authenticate_handshake.
		self.mcp_client: MCPClient | None = None
		# Active pipeline task for this conversation. Used to reject concurrent
		# prompts and to cancel in-flight work when the WebSocket closes.
		self.active_pipeline: asyncio.Task | None = None
		# Context for the currently-running pipeline. Exposed so a user-initiated
		# "cancel" message can flip should_stop without tearing down the connection.
		self.active_pipeline_ctx: PipelineContext | None = None

	async def send(self, message: dict):
		"""Send a message over the WebSocket.

		Raises WebSocketDisconnect (or similar) if the socket is closed. Callers
		that want to tolerate a dropped connection mid-pipeline (e.g., the
		`on_call` activity-stream callback) should wrap this in try/except -
		we don't swallow here because top-level pipeline steps need to know
		when delivery failed so they can exit cleanly.

		After a successful WS write, user-visible messages are persisted
		to the conversation's Redis stream so a reconnecting client can
		replay what it missed via ``resume``. Transport / meta messages
		(ack / ping / mcp_response / echo) are skipped - see
		_STREAM_SKIP_TYPES. Persist failures are logged at DEBUG and
		silently swallowed; an event not making it to the stream is
		tolerable, but propagating a Redis exception up into the
		pipeline's send path would cause phase-level failures.
		"""
		await self.websocket.send_json(message)

		if self.store is None or not self.conversation_id:
			return
		msg_type = message.get("type")
		if not msg_type or msg_type in self._STREAM_SKIP_TYPES:
			return
		try:
			await self.store.push_event(
				self.site_id, self.conversation_id, message,
			)
		except Exception as e:
			logger.debug(
				"push_event for type=%s conv=%s failed: %s",
				msg_type, self.conversation_id, e,
			)

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
		except TimeoutError:
			# Remember this msg_id so a late-arriving answer (after the
			# timeout fired but before the user gives up) can be acked
			# back to the UI instead of silently dropped.
			self._expired_questions[msg_id] = time.time()
			self._gc_expired_questions()
			return "[TIMEOUT] User did not respond. Consider escalating to a human operator."
		finally:
			self._pending_questions.pop(msg_id, None)

	def _gc_expired_questions(self) -> None:
		"""Drop entries older than the TTL. Called on every add so the map
		stays bounded even if the user never replies to any question."""
		cutoff = time.time() - _EXPIRED_Q_TTL_SECONDS
		stale = [k for k, ts in self._expired_questions.items() if ts < cutoff]
		for k in stale:
			self._expired_questions.pop(k, None)

	async def resolve_question(self, msg_id: str, answer: str) -> bool:
		"""Resolve a pending question with the user's answer.

		Returns True if the answer was delivered to a waiting Future.
		Returns False if the question is unknown OR if the question
		timed out recently (answer arrived late) - in the late case, an
		info message is sent back so the user knows the pipeline already
		proceeded without them.
		"""
		future = self._pending_questions.get(msg_id)
		if future and not future.done():
			future.set_result(answer)
			return True

		if msg_id in self._expired_questions:
			# Answer landed after the Future timed out. Tell the user so
			# they don't sit confused while the pipeline appears to ignore
			# them. GC the entry so the ack fires exactly once.
			self._expired_questions.pop(msg_id, None)
			try:
				await self.send({
					"msg_id": str(uuid.uuid4()),
					"type": "info",
					"data": {
						"message": (
							"Your response arrived after the clarifier timed out; "
							"the pipeline had to proceed without it. If you'd like "
							"to incorporate this answer, send it as a new prompt."
						),
						"code": "CLARIFIER_LATE_RESPONSE",
						"response_to": msg_id,
					},
				})
			except Exception as e:
				logger.debug("Failed to send late-clarifier info for %s: %s", msg_id, e)
		return False


async def _authenticate_handshake(
	websocket: WebSocket, conversation_id: str
) -> ConnectionState | None:
	"""Wait for and validate the handshake message."""
	try:
		raw = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
		handshake = json.loads(raw)
	except TimeoutError:
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason="Handshake timeout")
		except Exception:
			pass
		return None
	except WebSocketDisconnect:
		logger.debug("Client disconnected before handshake: conversation=%s", conversation_id)
		return None
	except Exception as e:
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason=f"Invalid handshake: {e}")
		except Exception:
			pass
		return None

	api_key = handshake.get("api_key", "")
	expected_key = websocket.app.state.settings.API_SECRET_KEY
	# Constant-time comparison defeats timing attacks - a naive != leaks the
	# prefix match length via response latency. hmac.compare_digest accepts
	# str/str or bytes/bytes, but TypeErrors on mismatched types if the
	# client sent a non-string, so we coerce both sides to str first.
	if not hmac.compare_digest(str(api_key), str(expected_key)):
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

	# Attach the state store if Redis is configured. ConnectionState.send
	# uses it to mirror user-visible events into the resume stream, and
	# the `resume` message handler reads from it.
	redis = getattr(websocket.app.state, "redis", None)
	store = None
	if redis is not None:
		from alfred.state.store import StateStore
		store = StateStore(redis)

	conn = ConnectionState(
		websocket=websocket,
		site_id=jwt_payload["site_id"],
		user=jwt_payload["user"],
		roles=jwt_payload["roles"],
		site_config=site_config,
		conversation_id=conversation_id,
		store=store,
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

	# Per-tool-call MCP timeout. Admin sets this via Alfred Settings ->
	# MCP Tool Timeout; the field ships in site_config at handshake. We
	# treat 0 / missing / negative / non-int the same way - fall back to
	# the 30s default. Clamping here means a sloppy admin config doesn't
	# disable timeouts entirely, which would hang pipelines on a dead
	# MCP server indefinitely.
	_cfg_timeout = site_config.get("mcp_timeout")
	try:
		_cfg_timeout = int(_cfg_timeout) if _cfg_timeout else 0
	except (TypeError, ValueError):
		_cfg_timeout = 0
	mcp_timeout_s = _cfg_timeout if _cfg_timeout > 0 else 30

	conn.mcp_client = MCPClient(
		send_func=conn.send,
		main_loop=asyncio.get_running_loop(),
		timeout=mcp_timeout_s,
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
		# Client reconnected after a WS drop. Replay every user-visible
		# event from the Redis stream that landed AFTER the last_msg_id
		# the client acknowledged receiving. Events in the stream are
		# serialised in the order they went out, so a simple linear
		# scan finds the right starting point.
		#
		# Contract: client MAY receive duplicates if it resumes with
		# last_msg_id=<something it already saw but never ack'd>. The
		# UI layer dedupes by msg_id, which is already guaranteed
		# unique per WS message by the server.
		last_msg_id = (data.get("data") or {}).get("last_msg_id")
		if not last_msg_id:
			# No anchor - nothing to replay. Could replay everything
			# but that's almost certainly not what the client wants
			# (would dump thousands of historical events on an open
			# tab). Silent no-op matches the old behaviour.
			return
		if conn.store is None or not conn.conversation_id:
			# Redis not configured or conn missing context. Can't
			# replay; silent no-op so the client's reconnect UX isn't
			# blocked on infra state.
			return

		try:
			events = await conn.store.get_events(
				conn.site_id, conn.conversation_id, since_id="0",
			)
		except Exception as e:
			logger.warning(
				"Resume replay for %s@%s conv=%s failed at get_events: %s",
				conn.user, conn.site_id, conversation_id, e,
			)
			return

		# Find the stream entry whose msg_id matches what the client
		# last saw. Start replay from the NEXT one.
		replay_start = None
		for idx, entry in enumerate(events):
			if entry["data"].get("msg_id") == last_msg_id:
				replay_start = idx + 1
				break

		if replay_start is None:
			# last_msg_id not in the stream window (either too old -
			# TTL or maxlen trimmed it - or never existed). Replay the
			# whole remaining window; the client will dedupe.
			replay_start = 0

		to_replay = events[replay_start:]
		logger.info(
			"Resume replay for %s@%s conv=%s: %d events after msg_id=%s",
			conn.user, conn.site_id, conversation_id,
			len(to_replay), last_msg_id,
		)

		# Send via the raw WS, NOT via conn.send(), so we don't
		# re-push events to the stream (that would duplicate every
		# replay into the stream).
		for entry in to_replay:
			try:
				await websocket.send_json(entry["data"])
			except Exception as e:
				logger.warning(
					"Resume replay send for %s@%s failed partway: %s",
					conn.user, conn.site_id, e,
				)
				return
		return

	if msg_type == "user_response":
		# User responding to a question from an agent
		response_to = data.get("data", {}).get("response_to", msg_id)
		answer = data.get("data", {}).get("text", "")
		await conn.resolve_question(response_to, answer)
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
		if not prompt_text:
			return

		# If the pipeline is paused waiting for a clarification answer, route
		# this prompt to the oldest pending question instead of starting a new
		# pipeline. This lets the user just type their answer in the chat box
		# without any special UI state - the frontend stays simple. Answers
		# do NOT count against the rate limit (they're continuation of a
		# running task, not a new one).
		if conn._pending_questions:
			oldest_id = next(iter(conn._pending_questions))
			logger.info(
				"Routing prompt as answer to pending question %s for %s@%s",
				oldest_id, conn.user, conn.site_id,
			)
			await conn.resolve_question(oldest_id, prompt_text)
			return

		# Rate limit: mirror the REST /api/v1/tasks behaviour. The REST path
		# caps prompts at `max_tasks_per_user_per_hour` per (site_id, user);
		# WS had no equivalent until this check, so an authenticated user
		# could open many conversations and burn LLM quota. Reuses the same
		# Redis sliding-window implementation in middleware/rate_limit.py
		# so REST and WS share one quota bucket per user.
		from alfred.middleware.rate_limit import (
			DEFAULT_MAX_TASKS_PER_HOUR,
			check_rate_limit,
		)
		max_per_hour = int(
			conn.site_config.get("max_tasks_per_user_per_hour")
			or DEFAULT_MAX_TASKS_PER_HOUR
		)
		allowed, remaining, retry_after = await check_rate_limit(
			websocket.app.state.redis,
			conn.site_id,
			conn.user,
			max_per_hour=max_per_hour,
		)
		if not allowed:
			logger.warning(
				"WS rate limit exceeded for %s@%s: retry_after=%ds",
				conn.user, conn.site_id, retry_after,
			)
			await websocket.send_json({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {
					"error": (
						f"You've hit the task rate limit ({max_per_hour}/hour). "
						f"Try again in {retry_after}s."
					),
					"code": "RATE_LIMIT",
					"retry_after": retry_after,
					"limit": max_per_hour,
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
			try:
				await _run_agent_pipeline(
					conn, conversation_id, prompt_text, manual_mode=manual_mode
				)
			finally:
				conn.active_pipeline = None

		conn.active_pipeline = spawn_logged(_run_and_clear(), name="pipeline-run")
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
):
	"""Run the full agent SDLC pipeline for a user prompt.

	Thin wrapper over `AgentPipeline` (Phase 3 #12 state machine). The
	orchestrator handles phase sequencing, tracer spans, and error boundaries.
	Adding a new phase: edit `alfred/api/pipeline.py`, not here.

	Args:
		manual_mode: The user's chat-mode selection from the UI switcher.
			One of "auto" | "dev" | "plan" | "insights". The orchestrator
			phase decides the final mode from this + prompt + memory.
	"""
	from alfred.api.pipeline import AgentPipeline, PipelineContext

	ctx = PipelineContext(
		conn=conn,
		conversation_id=conversation_id,
		prompt=prompt,
		manual_mode_override=manual_mode,
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
	except Exception:
		pass


@ws_router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
	"""WebSocket endpoint - authenticates, then routes messages and runs agent pipeline."""
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

	heartbeat_interval = websocket.app.state.settings.WS_HEARTBEAT_INTERVAL
	heartbeat_task = spawn_logged(
		_heartbeat_loop(websocket, heartbeat_interval),
		name="heartbeat-loop",
	)

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

		# Cancel any in-flight pipeline so orphaned crews don't keep burning
		# LLM calls after the user has already disconnected.
		if conn.active_pipeline and not conn.active_pipeline.done():
			logger.info("Cancelling in-flight pipeline for %s", conversation_id)
			conn.active_pipeline.cancel()
			try:
				await conn.active_pipeline
			except (asyncio.CancelledError, Exception):
				pass
