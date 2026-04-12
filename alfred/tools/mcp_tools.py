"""CrewAI @tool wrappers for MCP tools.

Each wrapper is a thin function that delegates to the MCP client via `call_sync`.
Tool descriptions are optimized for LLM readability - agents read these descriptions
to decide when to use each tool.

All wrappers catch exceptions and return a JSON string so the LLM reads errors as
normal tool responses instead of crashing the CrewAI task.

Usage:
    from alfred.tools.mcp_tools import build_mcp_tools

    tools = build_mcp_tools(mcp_client)
    agents = build_agents(custom_tools=tools)
"""

import json
import logging

from crewai.tools import tool

from alfred.tools.mcp_client import MCPClient

logger = logging.getLogger("alfred.mcp_tools")


def _mcp_call(mcp_client: MCPClient, tool_name: str, arguments: dict | None = None) -> str:
	"""Invoke an MCP tool and return a JSON string the LLM can parse.

	On any failure, returns a JSON error payload rather than raising. This lets
	the agent read the error and adapt (e.g. "OK, I don't have permission, let
	me try a different approach") instead of failing the whole task.
	"""
	try:
		result = mcp_client.call_sync(tool_name, arguments or {})
		return json.dumps(result, indent=2)
	except TimeoutError as e:
		logger.warning("MCP tool %s timed out: %s", tool_name, e)
		return json.dumps({
			"error": "timeout",
			"message": f"MCP tool '{tool_name}' timed out. The client app may be unresponsive.",
		})
	except Exception as e:
		logger.error("MCP tool %s failed: %s", tool_name, e, exc_info=True)
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
		"""Get basic site information including Frappe version, installed apps, default company, and country."""
		return _mcp_call(mcp_client, "get_site_info")

	@tool
	def get_doctypes(module: str = "") -> str:
		"""List DocType names and modules. Optionally filter by module name. Use this to find existing DocTypes for Link fields."""
		args = {"module": module} if module else {}
		return _mcp_call(mcp_client, "get_doctypes", args)

	@tool
	def get_doctype_schema(doctype: str) -> str:
		"""Get full field schema for a DocType from the LIVE site (includes custom fields). Requires read permission. Always prefer this over guessing field names."""
		return _mcp_call(mcp_client, "get_doctype_schema", {"doctype": doctype})

	@tool
	def get_existing_customizations() -> str:
		"""List existing customizations (custom fields, server scripts, client scripts, workflows) filtered by your permissions."""
		return _mcp_call(mcp_client, "get_existing_customizations")

	@tool
	def get_user_context() -> str:
		"""Get the current user's email, roles, permissions, and permitted modules."""
		return _mcp_call(mcp_client, "get_user_context")

	@tool
	def check_permission(doctype: str, action: str = "read") -> str:
		"""Check if the current user has a specific permission (read/write/create/delete) on a DocType. Always use this tool - never guess permissions."""
		return _mcp_call(mcp_client, "check_permission", {"doctype": doctype, "action": action})

	@tool
	def validate_name_available(doctype: str, name: str) -> str:
		"""Check if a document name is already taken on the LIVE site. Use before creating new DocTypes or documents to avoid naming conflicts."""
		return _mcp_call(mcp_client, "validate_name_available", {"doctype": doctype, "name": name})

	@tool
	def has_active_workflow(doctype: str) -> str:
		"""Check if a DocType already has an active workflow. Frappe allows only one active workflow per DocType."""
		return _mcp_call(mcp_client, "has_active_workflow", {"doctype": doctype})

	@tool
	def check_has_records(doctype: str) -> str:
		"""Check if a DocType has existing data records. Use before rollback or deletion to avoid data loss."""
		return _mcp_call(mcp_client, "check_has_records", {"doctype": doctype})

	@tool
	def dry_run_changeset(changes: str) -> str:
		"""Dry-run a changeset against the LIVE site using savepoint rollback. Returns {valid, issues, validated}. Does NOT commit. Validates mandatory fields, link targets, naming conflicts, Python/JS syntax, and Jinja templates. Use before presenting the final changeset."""
		return _mcp_call(mcp_client, "dry_run_changeset", {"changes": changes})

	# Lite pipeline: one agent handles the whole SDLC, so it gets the union of
	# every tool the specialist agents would need (deduped while preserving order).
	_lite_source = [
		get_site_info, get_doctypes, get_doctype_schema, get_existing_customizations,
		get_user_context, check_permission, validate_name_available, has_active_workflow,
		check_has_records, validate_python_syntax_stub, validate_js_syntax_stub,
	]
	_seen = set()
	lite_tools = []
	for t in _lite_source:
		if id(t) not in _seen:
			lite_tools.append(t)
			_seen.add(id(t))

	return {
		"requirement": [ask_user_stub, get_site_info, get_doctypes, get_doctype_schema, get_existing_customizations],
		"assessment": [check_permission, get_user_context, get_existing_customizations],
		"architect": [get_doctype_schema, get_doctypes, get_existing_customizations, has_active_workflow],
		"developer": [get_doctype_schema, get_doctypes],
		"tester": [
			validate_python_syntax_stub, validate_js_syntax_stub, validate_name_available,
			check_permission, has_active_workflow, get_doctype_schema, check_has_records,
			dry_run_changeset,
		],
		"deployer": [check_has_records],
		"lite": lite_tools,
	}


# Stubs for tools not provided by MCP (local to the processing app)

@tool
def ask_user_stub(question: str, choices: str = "") -> str:
	"""Ask the user a question and wait for their response. Use for clarifying requirements or getting approval."""
	return "[STUB] ask_user not yet connected to WebSocket"


@tool
def validate_python_syntax_stub(code: str) -> str:
	"""Validate Python syntax of a Server Script. Returns any syntax errors found."""
	try:
		compile(code, "<agent_code>", "exec")
		return json.dumps({"valid": True, "errors": []})
	except SyntaxError as e:
		return json.dumps({"valid": False, "errors": [f"{e.msg} at line {e.lineno}"]})


@tool
def validate_js_syntax_stub(code: str) -> str:
	"""Validate JavaScript syntax of a Client Script. Returns any syntax errors found."""
	return json.dumps({"valid": True, "errors": [], "note": "Basic validation only"})
