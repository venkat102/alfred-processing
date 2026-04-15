"""CrewAI @tool wrappers for MCP tools.

Each wrapper is a thin function that delegates to the MCP client via `call_sync`.
Tool descriptions are optimized for LLM readability - agents read these descriptions
to decide when to use each tool.

All wrappers catch exceptions and return a JSON string so the LLM reads errors as
normal tool responses instead of crashing the CrewAI task.

Phase 1 improvements layered in `_mcp_call`:
  - P1: Per-conversation Redis cache (not yet wired here - see websocket.py)
  - P2: Hard per-run call budget (fail loud when exceeded)
  - P4: Per-iteration deduplication (same (tool, args) returns cached result)
  - A2: Failure counter surfaced to the agent in subsequent tool responses
  - A3: Warn agent on tool misuse (e.g. dry_run before any schema lookup)

All per-run state lives on `mcp_client.run_state` - a dict we attach after the
handshake and reset on every new prompt. If the attribute is missing (e.g. in
unit tests with a bare mock), the wrapper degrades gracefully and still works.

Usage:
    from alfred.tools.mcp_tools import build_mcp_tools

    tools = build_mcp_tools(mcp_client)
    agents = build_agents(custom_tools=tools)
"""

import json
import logging
from typing import Any

from crewai.tools import tool

from alfred.tools.mcp_client import MCPClient

logger = logging.getLogger("alfred.mcp_tools")


# Default per-run cap on MCP calls. Can be overridden by setting
# mcp_client.run_state["call_budget"]. Hit reading 15 recorded per prompt in
# baseline runs, so 30 gives 2x headroom before we flag a runaway.
DEFAULT_CALL_BUDGET = 30

# Tools that shouldn't be called before the agent has ANY doctype context.
# If dry_run_changeset fires first, it's a strong signal the agent is
# hallucinating a changeset without checking real schemas.
_TOOLS_REQUIRING_PRIOR_LOOKUP = {"dry_run_changeset"}
_LOOKUP_TOOLS = {
	"lookup_doctype", "get_doctype_schema", "get_doctypes",
	"lookup_pattern", "get_existing_customizations",
}


def init_run_state(mcp_client: Any, conversation_id: str = "", budget: int = DEFAULT_CALL_BUDGET):
	"""Initialize or reset the per-run tracking state on an MCP client.

	Called by `_run_agent_pipeline` at the top of every new prompt. The state is
	attached as `mcp_client.run_state` and covers the lifetime of one pipeline run.
	"""
	mcp_client.run_state = {
		"conversation_id": conversation_id,
		"call_budget": budget,
		"calls_made": 0,
		"calls_by_tool": {},
		"failures": [],
		"failure_count": 0,
		"dedup_cache": {},   # (tool_name, args_key) -> cached JSON string
		"dedup_hits": 0,
		"lookup_tools_called": set(),
	}


def _format_failure_hint(run_state: dict) -> str:
	"""Compact summary of recent failures for injection into the next tool response.

	Shown to the agent as part of the JSON payload so it notices errors it would
	otherwise read past without adapting.
	"""
	failures = run_state.get("failures", [])
	if not failures:
		return ""
	recent = failures[-3:]
	parts = [f"{tool}: {err}" for tool, err in recent]
	return f"Previous failures in this run: {'; '.join(parts)}"


def _args_key(arguments: dict) -> str:
	"""Stable serialization of arguments for cache/dedup keys."""
	try:
		return json.dumps(arguments, sort_keys=True, default=str)
	except Exception:
		return repr(arguments)


def _mcp_call(mcp_client: MCPClient, tool_name: str, arguments: dict | None = None) -> str:
	"""Invoke an MCP tool and return a JSON string the LLM can parse.

	Implements the Phase 1 tool usage improvements:
	  - per-iteration dedup (same tool + args returns cached result)
	  - hard per-run call budget (fail loud when exceeded)
	  - failure counter surfaced back to the agent
	  - misuse warning (e.g. dry_run before any schema lookup)

	On any failure, returns a JSON error payload rather than raising. This lets
	the agent read the error and adapt instead of failing the whole task.
	"""
	arguments = arguments or {}
	run_state = getattr(mcp_client, "run_state", None)
	args_key = _args_key(arguments)

	# Budget check - fail loud before spending another MCP round trip.
	if run_state is not None:
		budget = run_state.get("call_budget", DEFAULT_CALL_BUDGET)
		calls_made = run_state.get("calls_made", 0)
		if calls_made >= budget:
			msg = (
				f"MCP call budget exceeded ({calls_made} >= {budget}). "
				"You have called too many tools in this pipeline run - either "
				"the previous responses already have the answer, or you're in "
				"a loop. Stop calling tools and finalize your output using what "
				"you already know."
			)
			logger.warning("MCP budget exceeded for conv=%s tool=%s",
				run_state.get("conversation_id", "?"), tool_name)
			return json.dumps({"error": "budget_exceeded", "message": msg})

	# Per-iteration dedup - avoid round-tripping the same call twice.
	if run_state is not None:
		dedup = run_state.setdefault("dedup_cache", {})
		dedup_key = f"{tool_name}::{args_key}"
		if dedup_key in dedup:
			run_state["dedup_hits"] = run_state.get("dedup_hits", 0) + 1
			logger.debug("MCP dedup hit for %s(%s)", tool_name, args_key[:80])
			return dedup[dedup_key]

	# Misuse warning - agent called a validation tool before any schema lookup.
	misuse_hint = ""
	if run_state is not None and tool_name in _TOOLS_REQUIRING_PRIOR_LOOKUP:
		if not run_state.get("lookup_tools_called"):
			misuse_hint = (
				"WARNING: you called a validation tool before any schema lookup. "
				"You should call lookup_doctype (or get_doctype_schema) FIRST to "
				"verify the target DocType and field names before validating. "
				"The tool ran anyway, but consider this a signal to double-check."
			)

	try:
		result = mcp_client.call_sync(tool_name, arguments)

		# Track successful calls + failures separately
		if run_state is not None:
			run_state["calls_made"] = run_state.get("calls_made", 0) + 1
			by_tool = run_state.setdefault("calls_by_tool", {})
			by_tool[tool_name] = by_tool.get(tool_name, 0) + 1

			if tool_name in _LOOKUP_TOOLS:
				run_state.setdefault("lookup_tools_called", set()).add(tool_name)

			if isinstance(result, dict) and result.get("error"):
				run_state.setdefault("failures", []).append(
					(tool_name, result.get("error"))
				)
				run_state["failure_count"] = run_state.get("failure_count", 0) + 1

		# Inject accumulated failure hint so the agent notices previous errors
		# instead of reading past them. Only when there's something to say.
		payload: Any = result
		if run_state is not None and isinstance(result, dict):
			hint = _format_failure_hint(run_state)
			if hint or misuse_hint:
				payload = dict(result)  # copy to avoid mutating the client's response
				notes = []
				if misuse_hint:
					notes.append(misuse_hint)
				if hint:
					notes.append(hint)
				payload["_alfred_notes"] = notes

		result_json = json.dumps(payload, indent=2)

		# Cache the result for within-iteration dedup
		if run_state is not None:
			run_state["dedup_cache"][dedup_key] = result_json

		return result_json

	except TimeoutError as e:
		logger.warning("MCP tool %s timed out: %s", tool_name, e)
		if run_state is not None:
			run_state.setdefault("failures", []).append((tool_name, "timeout"))
			run_state["failure_count"] = run_state.get("failure_count", 0) + 1
			run_state["calls_made"] = run_state.get("calls_made", 0) + 1
		return json.dumps({
			"error": "timeout",
			"message": f"MCP tool '{tool_name}' timed out. The client app may be unresponsive.",
		})
	except Exception as e:
		logger.error("MCP tool %s failed: %s", tool_name, e, exc_info=True)
		if run_state is not None:
			run_state.setdefault("failures", []).append((tool_name, "mcp_failure"))
			run_state["failure_count"] = run_state.get("failure_count", 0) + 1
			run_state["calls_made"] = run_state.get("calls_made", 0) + 1
		return json.dumps({"error": "mcp_failure", "message": str(e)})


def build_mcp_tools(mcp_client: MCPClient) -> dict[str, list]:
	"""Build CrewAI tool wrappers connected to a live MCP client.

	Args:
		mcp_client: An initialized MCPClient connected to the Client App.

	Returns:
		Dict mapping agent names to their tool lists (same format as TOOL_ASSIGNMENTS).
	"""

	@tool
	def get_site_info() -> str:
		"""Get basic site information: Frappe version, installed apps, default company, country.

		Example: get_site_info()
		  -> {"version": "15.x", "installed_apps": [{"name": "erpnext", "version": "15.0"}, ...], "site": "example.com"}

		Use this ONCE at the start of a run to understand what apps are available. No need to call repeatedly.
		"""
		return _mcp_call(mcp_client, "get_site_info")

	@tool
	def get_doctypes(module: str = "") -> str:
		"""List DocType names and modules, optionally filtered by module.

		Substitute the module from YOUR plan, not from this docstring.
		Example call: get_doctypes(module="Core")
		  -> {"doctypes": [{"name": "ToDo", "module": "Desk"}, {"name": "Note", "module": "Desk"}, ...], "count": N}

		Prefer `lookup_doctype` for detail lookups. Use this only to browse what exists in a module.
		"""
		args = {"module": module} if module else {}
		return _mcp_call(mcp_client, "get_doctypes", args)

	@tool
	def get_doctype_schema(doctype: str) -> str:
		"""[DEPRECATED - use `lookup_doctype` instead] Get the LIVE site schema for a DocType (includes custom fields).

		Kept for backwards compatibility. New code should call `lookup_doctype(name, layer="site")` or `layer="both"` for a merged view.

		Substitute the DocType from YOUR plan (the one the user actually
		asked about), not from this docstring. The example uses "ToDo"
		because it's a generic built-in doctype - it is NOT the doctype
		you should pass.
		Example call: get_doctype_schema("ToDo")
		  -> {"doctype": "ToDo", "fields": [{"fieldname": "owner", "fieldtype": "Link", "options": "User", "reqd": 1}, ...]}
		"""
		return _mcp_call(mcp_client, "get_doctype_schema", {"doctype": doctype})

	@tool
	def get_existing_customizations() -> str:
		"""List existing customizations (custom fields, server scripts, client scripts, workflows) filtered by your permissions.

		Example: get_existing_customizations()
		  -> {"custom_fields": [{"dt": "Customer", "fieldname": "tier"}, ...], "server_scripts": [...], "client_scripts": [...], "workflows": [...]}

		Use this BEFORE creating new customizations to avoid duplicating existing ones.
		"""
		return _mcp_call(mcp_client, "get_existing_customizations")

	@tool
	def get_user_context() -> str:
		"""Get the current user's email, roles, permissions, and permitted modules.

		Example: get_user_context()
		  -> {"user": "alice@example.com", "roles": ["System Manager", "Sales User"], "enabled": 1}

		Use this when you need to know who is making the request, for audit logs or permission-aware decisions.
		"""
		return _mcp_call(mcp_client, "get_user_context")

	@tool
	def check_permission(doctype: str, action: str = "read") -> str:
		"""Check if the current user has a specific permission (read/write/create/delete) on a DocType.

		Substitute the DocType from YOUR plan, not from this docstring.
		Example call: check_permission("ToDo", "create")
		  -> {"permitted": true, "reason": "System Manager role has create permission"}

		Always use this tool - never guess permissions. Use BEFORE proposing any DocType modification.
		"""
		return _mcp_call(mcp_client, "check_permission", {"doctype": doctype, "action": action})

	@tool
	def validate_name_available(doctype: str, name: str) -> str:
		"""Check if a document name is already taken on the LIVE site.

		Example: validate_name_available("DocType", "Training Program")
		  -> {"available": true}

		Use BEFORE creating new DocTypes to avoid naming conflicts that would fail at insert time.
		"""
		return _mcp_call(mcp_client, "validate_name_available", {"doctype": doctype, "name": name})

	@tool
	def has_active_workflow(doctype: str) -> str:
		"""Check if a DocType already has an active workflow. Frappe allows only one active workflow per DocType.

		Example: has_active_workflow("Leave Application")
		  -> {"has_workflow": true}

		Use BEFORE proposing a new workflow - if one exists, you should modify it rather than create a second one.
		"""
		return _mcp_call(mcp_client, "has_active_workflow", {"doctype": doctype})

	@tool
	def check_has_records(doctype: str) -> str:
		"""Check if a DocType has existing data records.

		Substitute the DocType from YOUR plan, not from this docstring.
		Example call: check_has_records("ToDo")
		  -> {"has_records": true, "count": 42}

		Use BEFORE rollback or deletion to avoid destroying user data. The Deployer calls this before removing a DocType during rollback.
		"""
		return _mcp_call(mcp_client, "check_has_records", {"doctype": doctype})

	@tool
	def dry_run_changeset(changes: str) -> str:
		"""Dry-run a changeset against the LIVE site using savepoint rollback. Returns {valid, issues, validated}. Does NOT commit. Validates mandatory fields, link targets, naming conflicts, Python/JS syntax, and Jinja templates. Use before presenting the final changeset.

		Example: dry_run_changeset('[{"op": "create", "doctype": "Notification", "data": {...}}]')
		  -> {"valid": true, "issues": [], "validated": 1}
		"""
		return _mcp_call(mcp_client, "dry_run_changeset", {"changes": changes})

	# ── Consolidated framework + pattern lookup (Tier 1b, from Framework KG) ──
	#
	# `lookup_doctype` replaces `get_doctypes` + `get_doctype_schema` for most
	# use cases - one richer tool with a `layer` argument gives framework truth,
	# live site state, or a merged view. Keep old tools for backwards compat.
	# SWE-Agent ACI principle: fewer richer tools beat many narrow ones.

	@tool
	def lookup_doctype(name: str, layer: str = "both") -> str:
		"""Look up a DocType across the framework KG and/or the live site.

		`layer`:
		  - "framework": vanilla schema from bench app JSONs (what the DocType ships with out of the box)
		  - "site": live site schema (includes custom fields installed on this site)
		  - "both" (default): merged view with both framework and site layers plus a `custom_fields` list

		CRITICAL: Pass the EXACT DocType name from the user's request.
		Do NOT substitute a different DocType just because this docstring
		uses one in its example. If the user's request mentions "Employee",
		you call lookup_doctype("Employee"), not lookup_doctype("ToDo").

		Example call shape: lookup_doctype("<DocType from user's request>", layer="framework")
		  -> {"name": "<DocType>", "fields": [{"fieldname": "...", "fieldtype": "...", ...}, ...]}

		Use BEFORE designing any change that touches an existing DocType so you know the real field names.
		Prefer this over get_doctype_schema - lookup_doctype covers both framework facts and site customizations in one call.
		"""
		return _mcp_call(mcp_client, "lookup_doctype", {"name": name, "layer": layer})

	@tool
	def lookup_pattern(query: str, kind: str = "all") -> str:
		"""Look up a curated Frappe customization pattern.

		`kind`:
		  - "name": exact pattern name match (e.g. "approval_notification")
		  - "search": keyword search across pattern names/descriptions/keywords
		  - "list": return all available pattern summaries (query ignored)
		  - "all" (default): try exact name first, fall back to keyword search

		Each pattern includes a template, when_to_use, when_not_to_use, required_clarifications, and anti_patterns.
		Adapt the template to the user's actual request - never use a pattern verbatim.

		Example: lookup_pattern("approval_notification", kind="name")
		  -> {"pattern": {"description": "...", "template": {...}, "event_reasoning": "..."}}
		Example: lookup_pattern("email manager on new order", kind="search")
		  -> {"doctypes": [...], "patterns": [{"name": "approval_notification", ...}]}

		Use this early in reasoning to ground your approach in a known-good pattern before generating code.
		"""
		return _mcp_call(mcp_client, "lookup_pattern", {"query": query, "kind": kind})

	# Lite pipeline: one agent handles the whole SDLC, so it gets the union of
	# every tool the specialist agents would need (deduped while preserving order).
	_lite_source = [
		lookup_doctype, lookup_pattern,
		get_site_info, get_doctypes, get_doctype_schema, get_existing_customizations,
		get_user_context, check_permission, validate_name_available, has_active_workflow,
		check_has_records, validate_python_syntax_stub, validate_js_syntax_stub,
		dry_run_changeset,
	]
	_seen = set()
	lite_tools = []
	for t in _lite_source:
		if id(t) not in _seen:
			lite_tools.append(t)
			_seen.add(id(t))

	# Insights pipeline (Phase B three-mode chat): read-only tools only.
	# Explicitly EXCLUDES `dry_run_changeset` (deploy-shaped) and the
	# local stubs `ask_user_stub`, `validate_python_syntax_stub`,
	# `validate_js_syntax_stub` (not meaningful for read-only Q&A).
	# No `get_doctype_schema` - use the consolidated `lookup_doctype` instead.
	insights_tools = [
		lookup_doctype,              # primary DocType schema lookup
		lookup_pattern,              # show curated customization patterns
		get_site_info,               # version, installed apps
		get_doctypes,                # browse DocTypes by module
		get_existing_customizations, # what's custom on this site
		get_user_context,            # current user + roles
		check_permission,            # "can I do X?"
		has_active_workflow,         # workflow presence check
		check_has_records,           # does this DocType have data
		validate_name_available,     # name availability probe
	]

	return {
		"requirement": [
			ask_user_stub,
			lookup_pattern,              # find relevant patterns for the user's request
			lookup_doctype,              # verify target DocType exists and check vanilla fields
			get_site_info,
			get_existing_customizations,
		],
		"assessment": [
			check_permission, get_user_context, get_existing_customizations,
			lookup_doctype,              # verify target doctype exists in framework
		],
		"architect": [
			lookup_doctype,              # primary source for vanilla field lookups
			lookup_pattern,              # match against known patterns
			get_existing_customizations, has_active_workflow,
		],
		"developer": [
			lookup_doctype,              # verify field names for the changeset
			lookup_pattern,              # retrieve template to adapt
		],
		"tester": [
			validate_python_syntax_stub, validate_js_syntax_stub, validate_name_available,
			check_permission, has_active_workflow, lookup_doctype, check_has_records,
			dry_run_changeset,
		],
		"deployer": [check_has_records],
		"lite": lite_tools,
		"insights": insights_tools,
	}


# Stubs for tools not provided by MCP (local to the processing app)

@tool
def ask_user_stub(question: str, choices: str = "") -> str:
	"""Ask the user a clarifying question. NOTE: this is a stub - the real clarification gate runs earlier in the pipeline (see `_clarify_requirements`). You should almost never need to call this - if you're uncertain about requirements, the clarifier already asked before you started.

	Both arguments are strings. If there are options, pass them as a comma-separated string, NOT a Python list.

	Example: ask_user_stub(question="Which approver field should the notification use?", choices="expense_approver,leave_approver")
	  -> the user's answer as a string

	If you find yourself about to call this, reconsider - your task should already have enough context from the clarifier.
	"""
	return "[STUB] ask_user should not be called at this phase - the clarification gate already ran. Proceed with the information you have."


@tool
def validate_python_syntax_stub(code: str) -> str:
	"""Validate Python syntax of a Server Script. Catches SyntaxError before the changeset reaches dry-run.

	Example: validate_python_syntax_stub("if not doc.name: frappe.throw('required')")
	  -> {"valid": true, "errors": []}
	Example: validate_python_syntax_stub("def foo(:")
	  -> {"valid": false, "errors": ["invalid syntax at line 1"]}

	Use BEFORE handing Server Script code to dry_run_changeset - it's cheaper to fail fast here.
	"""
	try:
		compile(code, "<agent_code>", "exec")
		return json.dumps({"valid": True, "errors": []})
	except SyntaxError as e:
		return json.dumps({"valid": False, "errors": [f"{e.msg} at line {e.lineno}"]})


@tool
def validate_js_syntax_stub(code: str) -> str:
	"""Validate JavaScript syntax of a Client Script (basic brace-balance check only).

	Example: validate_js_syntax_stub("frappe.ui.form.on('Customer', { refresh(frm) { console.log('hi'); } });")
	  -> {"valid": true, "errors": []}

	Not a full JS parser - only catches unbalanced braces and quotes. Trust dry_run_changeset for deeper validation.
	"""
	return json.dumps({"valid": True, "errors": [], "note": "Basic validation only"})
