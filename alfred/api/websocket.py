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

import ast
import asyncio
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from alfred.middleware.auth import verify_jwt_token

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


def _validate_changeset_shape(items: list[dict]) -> list[str]:
	"""Check each changeset item against the contract the deploy engine expects.

	Returns a list of human-readable error messages (empty if valid). Not a
	raising validator - callers use the errors list for logging and to decide
	whether to trigger the rescue path.

	Contract per item:
	  - op in {"create", "update", "delete"}
	  - doctype is a non-empty string
	  - data is a dict
	  - data.doctype matches the outer doctype when both are present
	"""
	errors = []
	valid_ops = {"create", "update", "delete"}
	for i, item in enumerate(items):
		if not isinstance(item, dict):
			errors.append(f"item[{i}] is {type(item).__name__}, expected dict")
			continue
		op = item.get("op")
		if op not in valid_ops:
			errors.append(f"item[{i}] has op={op!r}, expected one of {sorted(valid_ops)}")
		doctype = item.get("doctype")
		if not isinstance(doctype, str) or not doctype:
			errors.append(f"item[{i}] has doctype={doctype!r}, expected non-empty string")
		data = item.get("data", {})
		if not isinstance(data, dict):
			errors.append(f"item[{i}] has data={type(data).__name__}, expected dict")
			continue
		inner_dt = data.get("doctype")
		if inner_dt and isinstance(doctype, str) and inner_dt != doctype:
			errors.append(
				f"item[{i}] inner data.doctype={inner_dt!r} does not match outer {doctype!r}"
			)
	return errors


_CHAT_TEMPLATE_LEAKAGE = re.compile(
	r"<\|im_start\|>|<\|im_end\|>|<\|endoftext\|>|<\|start_header_id\|>|<\|end_header_id\|>|<\|eot_id\|>"
)
_CODE_FENCE_LINE = re.compile(r"^\s*```(?:json|python|javascript|js)?\s*$", re.MULTILINE)


def _parse_first_json_value(text: str):
	"""Parse the first well-formed JSON value in text.

	Walks the text, and at every `[` or `{` tries `JSONDecoder.raw_decode`.
	Returns the first successful parse, or None.

	This is the critical fix for qwen-style retry loops where the Developer
	task produces 5+ concatenated copies of the same changeset separated by
	prose and `<|im_start|>` leakage. A greedy regex can't handle that case
	because `json.loads` rejects concatenated top-level values as "Extra data".
	`raw_decode` stops at the first complete value and ignores the tail.

	Also runs `ast.literal_eval` as a fallback at each position so Python-repr
	dicts (single quotes, True/False/None) are handled the same way they used
	to be.
	"""
	if not text:
		return None
	decoder = json.JSONDecoder()
	for i, ch in enumerate(text):
		if ch not in "[{":
			continue
		try:
			obj, _ = decoder.raw_decode(text, i)
			return obj
		except json.JSONDecodeError:
			pass
		# ast fallback: walk to a balanced close and try literal_eval. Only
		# needed when the model emits Python dict repr instead of JSON.
		close = _find_balanced_close(text, i)
		if close is not None:
			try:
				return ast.literal_eval(text[i : close + 1])
			except (ValueError, SyntaxError):
				continue
	return None


def _find_balanced_close(text: str, start: int) -> int | None:
	"""Return the index of the `]` or `}` that closes the bracket at `start`.

	Single-pass scanner that tracks string state and escape sequences. Used
	by the ast fallback path; the primary parser uses raw_decode and doesn't
	need this.
	"""
	if start >= len(text) or text[start] not in "[{":
		return None
	open_char = text[start]
	close_char = "}" if open_char == "{" else "]"
	depth = 0
	in_string = False
	escape = False
	for i in range(start, len(text)):
		ch = text[i]
		if escape:
			escape = False
			continue
		if ch == "\\":
			escape = True
			continue
		if ch == '"':
			in_string = not in_string
			continue
		if in_string:
			continue
		if ch == open_char:
			depth += 1
		elif ch == close_char:
			depth -= 1
			if depth == 0:
				return i
	return None


def _extract_changes(result_text) -> list[dict]:
	"""Parse agent result text into normalized changeset items for the PreviewPanel.

	The PreviewPanel expects each item to have:
	  { op: "create", doctype: "Notification", data: { name: "...", fields: [...] } }

	Agent output varies (plan items, customizations_needed, flat dicts, markdown
	code fences), so we normalize everything into this format. Returns an empty
	list on any parse failure - the caller should treat empty as "extraction
	failed" and surface an error rather than silently showing nothing.

	Pre-parsing cleanup handles three kinds of noise that local models produce
	when they drift:
	  - Markdown code fences (```json ... ```), possibly multiple per output.
	  - Chat-template leakage tokens (`<|im_start|>`, `<|im_end|>`, ...) that
	    appear when the model hallucinates a new conversation turn past its
	    stop token.
	  - Repeated concatenated JSON blocks (qwen "fix the JSON" retry loops
	    sometimes produce 5+ identical copies of the same array). The parser
	    picks the first well-formed block via `JSONDecoder.raw_decode`.

	After extraction, runs `_validate_changeset_shape` to log any contract
	violations at WARNING level. Invalid items still pass through (so the
	rescue path has a chance) but the warnings surface in logs for debugging.
	"""
	if not result_text:
		logger.debug("_extract_changes: empty result_text")
		return []

	if not isinstance(result_text, str):
		result_text = str(result_text)

	try:
		cleaned = _CHAT_TEMPLATE_LEAKAGE.sub("", result_text)
		cleaned = _CODE_FENCE_LINE.sub("", cleaned)
		cleaned = cleaned.strip()

		parsed = _parse_first_json_value(cleaned)
		if parsed is None:
			logger.warning(
				"_extract_changes: no parseable JSON in result (first 500 chars): %r",
				result_text[:500],
			)
			return []
	except Exception as e:
		logger.exception("_extract_changes: unexpected error: %s", e)
		return []

	# Extract the items list from various agent output formats
	items = []

	def _looks_like_changeset_item(obj: object) -> bool:
		"""A dict that actually looks like a changeset item, not a
		line-item from a Sales Order / Quotation / Invoice example.

		Accepts:
		  - Proper changeset items with `op` or `operation`
		  - Requirement Analyst's `customizations_needed` entries with
		    `type` (which the normalizer remaps to `doctype`)
		  - Dicts with top-level `doctype` + nested `data` dict (some
		    agents omit `op` and imply create)

		Rejects line-item shapes like `{"item_code": "X", "qty": 10}`
		that have none of the above markers.
		"""
		if not isinstance(obj, dict):
			return False
		return (
			"op" in obj
			or "operation" in obj
			or "type" in obj
			or ("doctype" in obj and isinstance(obj.get("data"), dict))
		)

	if isinstance(parsed, list):
		items = parsed
	elif isinstance(parsed, dict):
		# Try the well-known list keys that CrewAI agents use for their
		# changeset output (Changeset.items pydantic model, plus older
		# orchestrator output shapes).
		for key in ("plan", "items", "customizations_needed", "execution_log", "changes"):
			if key in parsed and isinstance(parsed[key], list):
				candidate_list = parsed[key]
				# Sanity check: the extracted list must contain
				# changeset-shaped items. This prevents false positives
				# from documents that happen to have an `items` field of
				# LINE items (e.g. `{"doctype": "Sales Order", "items":
				# [{"item_code": "X", "qty": 10}]}` — the `items` list is
				# line items, NOT changeset operations).
				if candidate_list and all(
					_looks_like_changeset_item(it) for it in candidate_list
				):
					items = candidate_list
					break

		if not items:
			# Drift guard: a bare dict is only a valid single-item changeset
			# when it LOOKS like a changeset item. Otherwise it's likely
			# stray example JSON that leaked into the agent's prose Final
			# Answer (classic local-model drift: the agent explains what a
			# DocType contains and pastes a `{"doctype": "Sales Order",
			# "customer": "..."}` at the end). Coercing that into a create
			# op would silently deploy the wrong doctype. Refuse to extract
			# and let the rescue path regenerate from the original prompt.
			if _looks_like_changeset_item(parsed):
				items = [parsed]
			else:
				logger.warning(
					"_extract_changes: parsed a bare dict that does not look "
					"like a changeset item (keys=%s). Treating as drift and "
					"returning empty so the rescue path can run.",
					sorted(parsed.keys())[:10],
				)
				return []

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

	# Shape validation - surface contract violations in logs for debugging.
	# We still return the normalized list so the rescue path can run, but a
	# loud warning here means the agent's output drifted from the contract.
	errors = _validate_changeset_shape(normalized)
	if errors:
		logger.warning(
			"_extract_changes: %d contract violation(s) in %d item(s): %s",
			len(errors), len(normalized), "; ".join(errors[:5]),
		)

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
	except Exception as e:
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
		manual_mode = (data.get("data", {}).get("mode") or "auto").strip().lower()
		if manual_mode not in ("auto", "dev", "plan", "insights"):
			manual_mode = "auto"
		if not prompt_text:
			return

		# If the pipeline is paused waiting for a clarification answer, route
		# this prompt to the oldest pending question instead of starting a new
		# pipeline. This lets the user just type their answer in the chat box
		# without any special UI state - the frontend stays simple.
		if conn._pending_questions:
			oldest_id = next(iter(conn._pending_questions))
			logger.info(
				"Routing prompt as answer to pending question %s for %s@%s",
				oldest_id, conn.user, conn.site_id,
			)
			conn.resolve_question(oldest_id, prompt_text)
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
		# Cap the fix-task iterations so qwen-style repeat loops can't wedge
		# the pipeline. If it can't fix it in 3 ReAct steps, escalate to the
		# user rather than spending more tokens spinning.
		developer.max_iter = 3

		fix_task = Task(
			description=(
				"OUTPUT FORMAT (STRICT): Your entire Final Answer MUST be a single JSON\n"
				"array. Start with `[` and end with `]`. Do NOT repeat the array. Do NOT\n"
				"include markdown code fences. Do NOT include any prose, commentary, or\n"
				"duplicate copies. If you have nothing to change, output the input array\n"
				"unchanged - still as a single clean array.\n\n"
				"The changeset you produced failed dry-run validation against the live site. "
				"Fix the issues below and produce a corrected changeset.\n\n"
				f"Validation issues:\n{issues_json}\n\n"
				f"Original changeset:\n{changes_json}\n\n"
				"Return a corrected changeset as a JSON array of complete document definitions. "
				"Every 'data' object must include all required fields for its doctype. "
				"Use lookup_doctype(name, layer='framework') to verify field names before writing."
			),
			expected_output="A JSON array of corrected changeset items (single array, no repeats).",
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


async def _clarify_requirements(
	enhanced_prompt: str,
	conn: "ConnectionState",
	event_callback,
) -> tuple[str, list[tuple[str, str]]]:
	"""Structured clarification gate: ask the user about ambiguities before the crew runs.

	One direct LLM call asks "what ambiguities exist in this request that only
	a human can resolve?" and returns a JSON list of questions. If non-empty, we
	send each question to the UI via conn.ask_human() (which blocks until the
	user responds), then append the Q/A pairs to the enhanced prompt so the
	downstream agents have the clarified context.

	Principles:
	- Only blocking decisions (schema choices, trigger events, scope) warrant
	  a question. Not implementation details the agents can figure out.
	- Max 3 questions per pass to avoid interrogation fatigue.
	- Short timeout on each question (15 min) - if the user walks away, the
	  pipeline still completes using the LLM's best guess.
	- Never raises: on any error, returns the original prompt unchanged.

	Returns:
		Tuple of (clarified_prompt, qa_pairs). qa_pairs is the list of
		(question, answer) tuples so the caller can persist them into the
		conversation memory for future turns.
	"""
	if not enhanced_prompt:
		return enhanced_prompt, []

	try:
		import litellm

		site_config = conn.site_config or {}
		model = site_config.get("llm_model") or "ollama/llama3.1"
		api_key = site_config.get("llm_api_key") or ""
		base_url = site_config.get("llm_base_url") or ""

		system = (
			"You are a Frappe requirements analyst. You receive an enhanced user "
			"request and must identify BLOCKING ambiguities that only a human can "
			"resolve before any code is generated.\n\n"
			"A BLOCKING ambiguity is one where the wrong choice would force a rework. "
			"The categories you should look for:\n"
			"- Trigger timing: when should the customization fire? (on create / on save / "
			"on submit / on field change / scheduled - the wrong choice produces unwanted "
			"or missing events)\n"
			"- Recipient target: which user or role should receive a notification? When "
			"the request says 'the manager' or 'the approver', which specific Link field "
			"on the target DocType holds that user?\n"
			"- Scope: which DocType(s) does the customization apply to? When the "
			"user uses a category word ('orders', 'invoices', 'requests'), it may "
			"map to more than one Frappe DocType; flag the ambiguity so the user "
			"can name the exact target DocType.\n"
			"- Permissions: 'only X can do Y' - which exact role or permission?\n\n"
			"A NON-BLOCKING detail (do NOT ask about these):\n"
			"- Field labels / naming conventions (agent can decide)\n"
			"- Code style / implementation choices (agent can decide)\n"
			"- Stylistic UI wording (agent can decide)\n\n"
			"RULES:\n"
			"- Ask at most 3 questions. Usually zero is the right answer.\n"
			"- If the request is unambiguous, return an empty array.\n"
			"- Each question must be answerable in one short sentence.\n"
			"- Include a `choices` array with 2-4 concrete options when the answer "
			"has a finite set; omit or use [] if open-ended.\n\n"
			"OUTPUT FORMAT (STRICT): a raw JSON array, no prose, no fences:\n"
			'[{"question": "...", "choices": ["option A", "option B"]}, ...]\n'
			"If nothing to ask, output exactly `[]`."
		)

		kwargs = {
			"model": model,
			"messages": [
				{"role": "system", "content": system},
				{"role": "user", "content": f"ENHANCED REQUEST:\n{enhanced_prompt[:6000]}"},
			],
			"max_tokens": 1024,
			"temperature": 0.0,
			"timeout": 60,
		}
		if api_key:
			kwargs["api_key"] = api_key
		if base_url:
			kwargs["base_url"] = base_url
			kwargs["api_base"] = base_url
		num_ctx = int(site_config.get("llm_num_ctx") or 0)
		if num_ctx > 0:
			kwargs["num_ctx"] = num_ctx
		elif str(model).startswith("ollama/"):
			kwargs["num_ctx"] = 8192

		await event_callback("clarify", {
			"agent": "Requirements", "status": "checking",
			"message": "Checking for ambiguities that need your input...",
		})

		loop = asyncio.get_running_loop()

		def _run():
			resp = litellm.completion(**kwargs)
			return resp.choices[0].message.content or ""

		raw = await loop.run_in_executor(None, _run)
		logger.info("Clarify pass result (first 500): %r", (raw or "")[:500])

		questions = []
		try:
			cleaned = re.sub(r'^```(?:json)?\s*', '', (raw or "").strip())
			cleaned = re.sub(r'\s*```$', '', cleaned)
			match = re.search(r'\[.*\]', cleaned, re.DOTALL)
			if match:
				parsed = json.loads(match.group())
				if isinstance(parsed, list):
					questions = [q for q in parsed if isinstance(q, dict) and q.get("question")]
		except Exception as e:
			logger.warning("Clarify pass: failed to parse questions: %s", e)
			questions = []

		if not questions:
			logger.info("Clarify pass: no blocking ambiguities - proceeding directly")
			return enhanced_prompt, []

		questions = questions[:3]
		logger.info("Clarify pass: asking %d question(s)", len(questions))

		qa_pairs = []
		for q in questions:
			question_text = str(q.get("question", "")).strip()
			if not question_text:
				continue
			raw_choices = q.get("choices") or []
			choices = [str(c).strip() for c in raw_choices if str(c).strip()][:4]

			await event_callback("clarify", {
				"agent": "Requirements", "status": "asking",
				"message": question_text,
			})
			try:
				answer = await conn.ask_human(question_text, choices=choices, timeout=900)
			except Exception as e:
				logger.warning("ask_human failed for question %r: %s", question_text, e)
				answer = "[no response]"

			qa_pairs.append((question_text, answer or "[no response]"))

		if not qa_pairs:
			return enhanced_prompt, []

		clarifications_block = "\n\nUSER CLARIFICATIONS:\n" + "\n".join(
			f"Q: {q}\nA: {a}" for q, a in qa_pairs
		)
		clarified = enhanced_prompt + clarifications_block

		await event_callback("clarify", {
			"agent": "Requirements", "status": "done",
			"message": f"Got {len(qa_pairs)} clarification(s) - continuing with your input.",
		})

		return clarified, qa_pairs
	except Exception as e:
		logger.warning("Clarify pass crashed, proceeding with original prompt: %s", e, exc_info=True)
		return enhanced_prompt, []


async def _rescue_regenerate_changeset(
	original_prompt: str,
	failed_output: str,
	site_config: dict,
	event_callback,
	user_prompt: str = "",
	drift_reason: str | None = None,
) -> list[dict]:
	"""Last-resort rescue: regenerate the changeset from scratch when the crew drifted.

	Local coder models (qwen2.5-coder, llama3.1) sometimes pivot into explanatory
	mode after calling get_doctype_schema - they describe what they found instead
	of emitting a changeset. When that happens, we run ONE direct LLM call with
	the original user prompt + the failed attempt, asking for a clean JSON
	changeset in one shot. No tools, no agent loop - a single focused call.

	Args:
		original_prompt: The enhanced prompt fed to the crew. Used as the
			primary source of truth for what the user wants.
		failed_output: The developer agent's raw Final Answer (the drift).
			INTENTIONALLY OMITTED from the rescue LLM prompt when
			`drift_reason` is set - the drifted prose is noise and would
			just re-anchor the rescue LLM on the wrong content.
		site_config: LLM config (model, api_key, base_url, num_ctx).
		event_callback: For streaming "Rescue" events to the UI.
		user_prompt: The user's ORIGINAL raw prompt (pre-enhancement). Used
			to inject a hard DocType constraint into the rescue system
			message. This is the most reliable signal for "what the user
			actually asked about".
		drift_reason: If the caller already detected drift (see
			`alfred.api.pipeline._detect_drift`), pass the reason here.
			When set:
			  - The failed_output is NOT included in the rescue LLM prompt
			    (it would just re-anchor on the drift).
			  - A specific "your upstream drifted, do NOT copy it" note is
			    prepended to the rescue system message.

	Returns a normalized changeset list (possibly empty) - never raises.
	"""
	if not original_prompt:
		return []

	try:
		import litellm

		model = site_config.get("llm_model") or "ollama/llama3.1"
		api_key = site_config.get("llm_api_key") or ""
		base_url = site_config.get("llm_base_url") or ""

		# Drift-aware preamble. When the upstream crew produced drift, we
		# tell the rescue LLM explicitly that a previous attempt went
		# off-topic, and we refuse to pass the failed output through.
		drift_preamble = ""
		if drift_reason:
			drift_preamble = (
				"URGENT - DRIFT RECOVERY MODE:\n"
				f"A previous attempt drifted off-topic ({drift_reason}). That "
				"drifted output is NOT shown to you here because it would just "
				"re-anchor your answer. Read ONLY the USER REQUEST below and "
				"generate a fresh changeset that targets the DocType named in "
				"the user's request. Do not reference Sales Order, Expense "
				"Claim, or any other DocType the user did not explicitly name.\n\n"
			)

		raw_user_ref = (user_prompt or original_prompt).strip()

		system = (
			drift_preamble
			+ "You are a Frappe changeset generator. Given a user request, produce a "
			"deployable JSON changeset that can be applied via frappe.get_doc(data).insert().\n\n"
			"OUTPUT FORMAT (STRICT):\n"
			"- Output ONLY a raw JSON array. No prose, no markdown, no code fences, no commentary.\n"
			"- Start with `[` and end with `]`.\n"
			"- Each item: {\"op\": \"create\", \"doctype\": \"<type>\", \"data\": {...complete document...}}\n"
			"- Every `data` object MUST include \"doctype\" matching the outer doctype plus all "
			"mandatory fields for that document type.\n\n"
			"STAY IN THE USER'S DOMAIN - CRITICAL:\n"
			"- The user's ORIGINAL raw request is pinned at the top of the user message below "
			"under 'USER RAW REQUEST'. The target DocType is whichever one the user named THERE.\n"
			"- If the user said 'Employee', emit items targeting Employee. If the user said "
			"'Leave Application', emit items targeting Leave Application. Do NOT substitute.\n"
			"- Read the USER RAW REQUEST TWICE before deciding on the target DocType.\n\n"
			"FRAPPE CUSTOMIZATION BASICS:\n"
			"- Email alert -> use Notification doctype (not Server Script)\n"
			"- New field on existing doctype -> Custom Field (not a new DocType)\n"
			"- Validation rule / business rule / custom constraint / 'throw a message' ->\n"
			"  Server Script (script_type='DocType Event', doctype_event='before_save' or\n"
			"  'before_insert' or 'validate'). The script body uses `doc.<field>` and calls\n"
			"  `frappe.throw('<user-facing message>')` to reject the save.\n"
			"  A validation is NEVER a Notification and NEVER a new DocType.\n"
			"- Required fields for Notification: name, subject, document_type, event, channel, "
			"recipients, message, enabled\n"
			"- Required fields for Server Script: name, script_type, reference_doctype, "
			"doctype_event, script\n"
			"- Required fields for Custom Field: name, dt, fieldname, fieldtype, label\n\n"
			"SHAPE OF EACH ITEM (placeholders in <angle brackets>):\n"
			'[{"op":"create","doctype":"<DocType kind>","data":{"doctype":"<DocType kind>","name":"<descriptive name>","...mandatory fields..."}}]\n\n'
			"If the request genuinely cannot be satisfied with any Frappe customization, "
			"output `[]`."
		)

		user_msg_parts = [
			f"USER RAW REQUEST (this is the authoritative source of target DocType and intent):\n{raw_user_ref[:2000]}",
		]
		if original_prompt and original_prompt != raw_user_ref:
			user_msg_parts.append(
				f"\n\nENHANCED SPEC (rewritten but must not contradict the raw request above):\n{original_prompt[:2000]}"
			)
		# Only include the failed output if drift was NOT detected. If
		# drift was detected, the prose is actively harmful - it would
		# just re-anchor the rescue LLM on the wrong content.
		if failed_output and not drift_reason:
			user_msg_parts.append(
				"\n\nThe agent pipeline produced this output, which is not a valid changeset - "
				"you may use it as hints but IGNORE its format:\n" + failed_output[:3000]
			)
		user_msg_parts.append("\n\nProduce the JSON changeset now. Raw JSON only.")

		kwargs = {
			"model": model,
			"messages": [
				{"role": "system", "content": system},
				{"role": "user", "content": "".join(user_msg_parts)},
			],
			"max_tokens": 2048,
			"temperature": 0.0,
			"timeout": 90,
		}
		if api_key:
			kwargs["api_key"] = api_key
		if base_url:
			kwargs["base_url"] = base_url
			kwargs["api_base"] = base_url
		num_ctx = int(site_config.get("llm_num_ctx") or 0)
		if num_ctx > 0:
			kwargs["num_ctx"] = num_ctx
		elif str(model).startswith("ollama/"):
			kwargs["num_ctx"] = 8192

		await event_callback("reformat", {
			"agent": "Rescue", "status": "running",
			"message": "Crew drifted off-spec. Regenerating changeset from the original request...",
		})

		loop = asyncio.get_running_loop()

		def _run():
			resp = litellm.completion(**kwargs)
			return resp.choices[0].message.content or ""

		regenerated = await loop.run_in_executor(None, _run)
		logger.info(
			"Rescue regeneration result (first 500): %r", (regenerated or "")[:500],
		)
		changes = _extract_changes(regenerated)
		if changes:
			await event_callback("reformat", {
				"agent": "Rescue", "status": "success",
				"message": f"Rescue produced {len(changes)} item(s).",
			})
		else:
			await event_callback("reformat", {
				"agent": "Rescue", "status": "empty",
				"message": "Rescue pass produced no changeset - request may be out of scope.",
			})
		return changes
	except Exception as e:
		logger.warning("Rescue regeneration failed: %s", e, exc_info=True)
		return []


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
	await AgentPipeline(ctx).run()


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
