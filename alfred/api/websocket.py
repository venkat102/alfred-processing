"""WebSocket handler for real-time bidirectional communication with client apps.

Protocol:
1. Client connects to /ws/{conversation_id}
2. Client sends handshake: {"api_key": "...", "jwt_token": "...", "site_config": {...}}
3. Server validates API key + JWT, extracts site_id and user
4. Bidirectional messaging begins - each message has a msg_id for ack tracking
5. MCP (JSON-RPC) messages are identified by "jsonrpc" field, all others by "type" field
6. Heartbeat ping every 30 seconds
7. On disconnect, unacked messages buffered in Redis for replay on reconnect
"""

import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from alfred.middleware.auth import verify_jwt_token
from alfred.state.store import StateStore

if TYPE_CHECKING:
	from alfred.agents.crew import CrewState
	from alfred.tools.mcp_client import MCPClient

logger = logging.getLogger("alfred.websocket")


# Human-readable descriptions for MCP tool calls, used to render a live
# activity ticker in the UI while agents process. Keep these terse - they
# appear in a single-line status row.
_TOOL_ACTIVITY = {
	"get_site_info": lambda a: "Reading site info",
	"get_doctypes": lambda a: (
		f"Listing DocTypes in {a['module']}" if a.get("module") else "Listing DocTypes"
	),
	"get_doctype_schema": lambda a: f"Reading {a.get('doctype', 'DocType')} schema",
	"get_existing_customizations": lambda a: "Scanning existing customizations",
	"get_user_context": lambda a: "Checking user context",
	"check_permission": lambda a: (
		f"Checking {a.get('action', 'read')} permission on {a.get('doctype', '?')}"
	),
	"validate_name_available": lambda a: (
		f"Checking if '{a.get('name', '?')}' is available as {a.get('doctype', '?')}"
	),
	"has_active_workflow": lambda a: f"Checking for active workflow on {a.get('doctype', '?')}",
	"check_has_records": lambda a: f"Checking for existing records in {a.get('doctype', '?')}",
	"dry_run_changeset": lambda a: "Validating changeset against live site",
}


def _describe_tool_call(tool_name: str, arguments: dict) -> str:
	"""Return a human-readable one-line description of an MCP tool call."""
	formatter = _TOOL_ACTIVITY.get(tool_name)
	if formatter is None:
		return f"Running {tool_name}"
	try:
		return formatter(arguments or {})
	except Exception:
		return f"Running {tool_name}"


def _extract_changes(result_text: str) -> list[dict]:
	"""Parse agent result text into normalized changeset items for the PreviewPanel.

	The PreviewPanel expects each item to have:
	  { op: "create", doctype: "Notification", data: { name: "...", fields: [...] } }

	Agent output varies (plan items, customizations_needed, flat dicts, markdown
	code fences), so we normalize everything into this format. Returns an empty
	list on any parse failure - the caller should treat empty as "extraction
	failed" and surface an error rather than silently showing nothing.
	"""
	if not result_text:
		logger.debug("_extract_changes: empty result_text")
		return []

	try:
		# Strip markdown code fences (```json ... ```)
		cleaned = re.sub(r'^```(?:json)?\s*', '', result_text.strip())
		cleaned = re.sub(r'\s*```$', '', cleaned)

		# Try to find JSON object or array. Greedy match so nested braces work.
		json_match = re.search(r'[\[{].*[\]}]', cleaned, re.DOTALL)
		if not json_match:
			logger.warning(
				"_extract_changes: no JSON found in result (first 200 chars): %r",
				result_text[:200],
			)
			return []

		parsed = json.loads(json_match.group())
	except json.JSONDecodeError as e:
		logger.warning(
			"_extract_changes: JSON decode failed at pos %d: %s. Text (first 200): %r",
			getattr(e, "pos", -1), e.msg, result_text[:200],
		)
		return []
	except Exception as e:
		logger.exception("_extract_changes: unexpected error: %s", e)
		return []

	# Extract the items list from various agent output formats
	items = []
	if isinstance(parsed, list):
		items = parsed
	elif isinstance(parsed, dict):
		for key in ("plan", "items", "customizations_needed", "execution_log", "changes"):
			if key in parsed and isinstance(parsed[key], list):
				items = parsed[key]
				break
		if not items:
			items = [parsed]

	# Normalize each item into { op, doctype, data: { name, ... } }
	normalized = []
	for item in items:
		if not isinstance(item, dict):
			continue

		op = item.get("op") or item.get("operation") or "create"
		doctype = item.get("doctype") or item.get("type") or "Other"
		name = item.get("name") or item.get("data", {}).get("name") or ""

		# If item already has a nested "data" dict, use it
		data = item.get("data", {})
		if not isinstance(data, dict):
			data = {}

		# Ensure name is in data
		if name and not data.get("name"):
			data["name"] = name

		# If there's a description but no data.name, use description
		if not data.get("name") and item.get("description"):
			data["name"] = item.get("description")

		# Copy useful top-level fields into data if not already there
		for field in ("fields", "script", "permissions", "description", "event", "channel"):
			if field in item and field not in data:
				data[field] = item[field]

		normalized.append({
			"op": op,
			"doctype": doctype,
			"data": data,
		})

	return normalized

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
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason="Handshake timeout")
		except Exception:
			pass
		return None
	except WebSocketDisconnect:
		logger.debug("Client disconnected before handshake: conversation=%s", conversation_id)
		return None
	except (json.JSONDecodeError, Exception) as e:
		try:
			await websocket.close(code=WS_CLOSE_INVALID_HANDSHAKE, reason=f"Invalid handshake: {e}")
		except Exception:
			pass
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

	if msg_type == "prompt":
		# Core pipeline: user sent a prompt - run the agent crew
		prompt_text = data.get("data", {}).get("text", "")
		if not prompt_text:
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
				await _run_agent_pipeline(conn, conversation_id, prompt_text)
			finally:
				conn.active_pipeline = None

		conn.active_pipeline = asyncio.create_task(_run_and_clear())
		return

	logger.info("Custom message from %s@%s: type=%s", conn.user, conn.site_id, msg_type)

	# Unknown type - echo back
	await websocket.send_json({
		"msg_id": str(uuid.uuid4()),
		"type": "echo",
		"data": {"received_type": msg_type, "received_msg_id": msg_id},
	})


async def _dry_run_with_retry(
	conn: "ConnectionState",
	state: "CrewState",
	changes: list[dict],
	site_config: dict,
	event_callback,
) -> dict:
	"""Run dry-run validation on a changeset. On failure, self-heal once by
	re-running just the Developer agent with the issues as context.

	Returns a dict shaped like:
		{
			"valid": bool,
			"issues": list[dict],
			"validated": int,
			"_final_changes": list[dict]   # the changeset to actually show the user
		}

	Never raises - on any failure (MCP call failed, retry crashed, etc.) it
	returns a best-effort result so the pipeline can still send a preview.
	"""
	if not conn.mcp_client:
		logger.info("Skipping dry-run: no MCP client on connection")
		return {"valid": True, "issues": [], "validated": 0, "_final_changes": changes}

	async def _run(changeset):
		"""Call the dry_run_changeset MCP tool and normalize the response.

		If the MCP call itself fails (connection, permission, tool not registered),
		return valid=False with a single "infrastructure" issue so the user knows
		validation didn't actually run, rather than silently showing a green badge.
		"""
		try:
			result = await conn.mcp_client.call_tool(
				"dry_run_changeset", {"changes": changeset}
			)
			if not isinstance(result, dict):
				return {
					"valid": False, "validated": 0,
					"issues": [{
						"severity": "warning",
						"issue": f"dry_run_changeset returned unexpected type: {type(result).__name__}",
					}],
				}
			# The client-side _safe_execute wrapper returns {"error": "...", "message": "..."}
			# on permission denied / not found / internal error. Treat as not-validated.
			if result.get("error"):
				return {
					"valid": False, "validated": 0,
					"issues": [{
						"severity": "warning",
						"issue": f"Validation could not run: {result.get('message', result['error'])}",
					}],
				}
			return result
		except Exception as e:
			logger.warning("Dry-run MCP call failed: %s", e)
			return {
				"valid": False, "validated": 0,
				"issues": [{"severity": "warning", "issue": f"Validation infrastructure error: {e}"}],
			}

	await event_callback("validation", {
		"agent": "Validator", "status": "dry_run_running",
		"message": "Validating changeset against live site...",
	})

	dry_run = await _run(changes)

	if dry_run.get("valid") or state.dry_run_retries >= 1:
		dry_run["_final_changes"] = changes
		return dry_run

	# Self-heal: re-run the Developer agent once with the validation issues injected
	state.dry_run_retries = 1
	issues_json = json.dumps(dry_run.get("issues", []), indent=2)
	changes_json = json.dumps(changes, indent=2)

	await event_callback("validation", {
		"agent": "Developer", "status": "dry_run_retry",
		"message": f"Dry-run found {len(dry_run.get('issues', []))} issue(s). Asking Developer to fix...",
	})

	try:
		from crewai import Crew, Process, Task
		from alfred.agents.definitions import build_agents
		from alfred.tools.mcp_tools import build_mcp_tools

		custom_tools = build_mcp_tools(conn.mcp_client)
		agents = build_agents(site_config=site_config, custom_tools=custom_tools)
		developer = agents["developer"]

		fix_task = Task(
			description=(
				"The changeset you produced failed dry-run validation against the live site. "
				"Fix the issues below and produce a corrected changeset.\n\n"
				f"Validation issues:\n{issues_json}\n\n"
				f"Original changeset:\n{changes_json}\n\n"
				"Return a corrected changeset as a JSON array of complete document definitions. "
				"Every 'data' object must include all required fields for its doctype. "
				"Use get_doctype_schema to verify field names against the live site before writing."
			),
			expected_output="A JSON array of corrected changeset items",
			agent=developer,
		)

		fix_crew = Crew(
			agents=[developer],
			tasks=[fix_task],
			process=Process.sequential,
			memory=False,
			verbose=False,
		)

		loop = asyncio.get_running_loop()
		retry_result = await loop.run_in_executor(None, fix_crew.kickoff)
		retry_text = str(retry_result) if retry_result else ""
		fixed_changes = _extract_changes(retry_text)

		if fixed_changes:
			changes = fixed_changes
			dry_run = await _run(fixed_changes)
		else:
			await event_callback("validation", {
				"agent": "Developer", "status": "dry_run_retry_empty",
				"message": "Retry produced no parseable changeset. Showing original issues.",
			})
	except Exception as e:
		logger.error("Dry-run retry failed: %s", e, exc_info=True)
		await event_callback("validation", {
			"agent": "Developer", "status": "dry_run_retry_failed",
			"message": f"Retry crashed: {e}. Showing original issues.",
		})
		# Keep original changes + original dry_run issues

	dry_run["_final_changes"] = changes
	return dry_run


async def _run_agent_pipeline(conn: ConnectionState, conversation_id: str, prompt: str):
	"""Run the full agent SDLC pipeline for a user prompt.

	This is the core integration point - it connects the WebSocket
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

	# Pipeline mode override from the admin portal. The portal's check_plan
	# response may include {"pipeline_mode": "lite" | "full"} to force a mode
	# based on the site's subscription tier. If present, this overrides the
	# site's local Alfred Settings.pipeline_mode. If not present, the site's
	# setting wins. If the admin portal isn't configured at all, we fall back
	# to the site's setting.
	plan_pipeline_mode: str | None = None

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
			# Capture any plan-level pipeline mode override
			_raw_mode = (plan_result.get("pipeline_mode") or "").lower()
			if _raw_mode in ("full", "lite"):
				plan_pipeline_mode = _raw_mode
		except Exception as e:
			logger.warning("Plan check failed (allowing by default): %s", e)

	# Step 3: Enhance the prompt
	await conn.send({
		"msg_id": str(uuid.uuid4()),
		"type": "agent_status",
		"data": {"agent": "System", "status": "enhancing", "message": "Analyzing your request..."},
	})

	user_context = {
		"user": conn.user,
		"roles": conn.roles,
		"site_id": conn.site_id,
	}

	from alfred.agents.prompt_enhancer import enhance_prompt
	enhanced_prompt = await enhance_prompt(prompt, user_context, conn.site_config)

	# Determine pipeline mode (full 6-agent SDLC vs single-agent lite).
	# Precedence (highest to lowest):
	#   1. Admin portal plan override (plan_pipeline_mode) - forces the mode
	#      based on subscription tier, so a "starter" plan is locked to lite
	#      regardless of what the site admin toggles.
	#   2. site_config.pipeline_mode - self-hosted / no-portal installs use
	#      the Alfred Settings field to pick their own mode.
	#   3. Default "full" - safest for complex tasks when neither is set.
	if plan_pipeline_mode:
		pipeline_mode = plan_pipeline_mode
		pipeline_mode_source = "plan"
	else:
		pipeline_mode = (conn.site_config.get("pipeline_mode") or "full").lower()
		if pipeline_mode not in ("full", "lite"):
			pipeline_mode = "full"
		pipeline_mode_source = "site_config"

	logger.info(
		"Pipeline mode resolved for %s: %s (source=%s)",
		conn.site_id, pipeline_mode, pipeline_mode_source,
	)

	# Step 4: Notify user that agent processing has started. Include the mode
	# so the UI can render a "Basic" badge and hide the 6-phase pipeline.
	await conn.send({
		"msg_id": str(uuid.uuid4()),
		"type": "agent_status",
		"data": {
			"agent": "Orchestrator",
			"status": "started",
			"phase": "requirement",
			"pipeline_mode": pipeline_mode,
			"pipeline_mode_source": pipeline_mode_source,
		},
	})

	# Step 5: Build and run the crew
	try:
		from alfred.agents.crew import build_alfred_crew, build_lite_crew, run_crew, load_crew_state
		from alfred.tools.mcp_tools import build_mcp_tools

		# Check for existing state (resumption)
		previous_state = None
		if store:
			previous_state = await load_crew_state(store, conn.site_id, conversation_id)

		# Build live MCP-backed tools so agents query the real Client App site
		# (with correct permissions) instead of hardcoded stubs.
		custom_tools = build_mcp_tools(conn.mcp_client) if conn.mcp_client else None

		if pipeline_mode == "lite":
			lite_tools = (custom_tools or {}).get("lite", []) if custom_tools else []
			if not lite_tools:
				logger.warning(
					"Lite pipeline starting without MCP tools for %s - the agent "
					"will have no way to verify DocType schemas, check permissions, "
					"or run dry_run_changeset. Expect degraded output quality. "
					"This usually means mcp_client is None (handshake did not "
					"initialize it) or build_mcp_tools returned no 'lite' key.",
					conn.site_id,
				)
			crew, state = build_lite_crew(
				user_prompt=enhanced_prompt,
				user_context=user_context,
				site_config=conn.site_config,
				previous_state=previous_state,
				lite_tools=lite_tools,
			)
		else:
			crew, state = build_alfred_crew(
				user_prompt=enhanced_prompt,
				user_context=user_context,
				site_config=conn.site_config,
				previous_state=previous_state,
				custom_tools=custom_tools,
			)

		# Event callback - streams agent events to the user via WebSocket
		async def event_callback(event_type: str, data: dict):
			await conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "agent_status",
				"data": {"event": event_type, **data},
			})

		# Run with timeout - sequential process is faster, so 2× per-task is enough
		timeout = conn.site_config.get("task_timeout_seconds", 300)
		result = await asyncio.wait_for(
			run_crew(crew, state, store, conn.site_id, conversation_id, event_callback),
			timeout=timeout * 2,
		)

		# Step 6: Send result back
		if result["status"] == "completed":
			result_text = result.get("result", "")

			# Send the completed status
			await conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "agent_status",
				"data": {"agent": "Orchestrator", "status": "completed", "result": result_text[:2000]},
			})

			# Send the changeset as a preview so the UI can display it.
			# Parse the result JSON and normalize each item to the format
			# the PreviewPanel expects:
			#   { op: "create", doctype: "Notification", data: { name: "...", fields: [...], ... } }
			changes = _extract_changes(result_text)

			if changes:
				# Pre-preview dry-run via MCP: users should only see validated changesets.
				dry_run = await _dry_run_with_retry(
					conn, state, changes, conn.site_config, event_callback
				)
				changes = dry_run.pop("_final_changes", changes)

				await conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "changeset",
					"data": {
						"conversation": conversation_id,
						"changes": changes,
						"result_text": result_text[:4000],
						"dry_run": dry_run,
					},
				})
			else:
				# Crew completed but produced no parseable changeset. Surface a
				# clear error instead of leaving the UI in processing state -
				# the polling fallback would eventually notice nothing arrived,
				# but an explicit error lets the user retry immediately.
				logger.warning(
					"Pipeline completed but _extract_changes returned empty. "
					"Result text (first 500): %r", result_text[:500],
				)
				await conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "error",
					"data": {
						"error": (
							"The agent pipeline completed but didn't produce a valid "
							"changeset. Try rephrasing your request or check the processing "
							"app logs."
						),
						"code": "EMPTY_CHANGESET",
						"result_preview": result_text[:500],
					},
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
			"data": {"error": "Pipeline timed out. The conversation has been saved - you can resume later.", "code": "PIPELINE_TIMEOUT"},
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

		# Cancel any in-flight pipeline so orphaned crews don't keep burning
		# LLM calls after the user has already disconnected.
		if conn.active_pipeline and not conn.active_pipeline.done():
			logger.info("Cancelling in-flight pipeline for %s", conversation_id)
			conn.active_pipeline.cancel()
			try:
				await conn.active_pipeline
			except (asyncio.CancelledError, Exception):
				pass
