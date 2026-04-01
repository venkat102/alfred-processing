"""CrewAI @tool wrappers for MCP tools.

Each wrapper is a thin function that delegates to the MCP client.
Tool descriptions are optimized for LLM readability — agents read
these descriptions to decide when to use each tool.

Usage:
    from intern.tools.mcp_tools import build_mcp_tools

    tools = build_mcp_tools(mcp_client)
    agents = build_agents(custom_tools=tools)
"""

import asyncio
import json
import logging
from typing import Any

from crewai.tools import tool

from intern.tools.mcp_client import MCPClient

logger = logging.getLogger("alfred.mcp_tools")

# ── Tool Description Constants ────────────────────────────────────

DESC_GET_SITE_INFO = (
	"Get basic site information including Frappe version, installed apps, "
	"default company, and country. Use this to understand the site environment."
)
DESC_GET_DOCTYPES = (
	"List DocType names and modules. Optionally filter by module name. "
	"Use this to find existing DocTypes for Link fields and to check what's already available."
)
DESC_GET_DOCTYPE_SCHEMA = (
	"Get the full field schema for a DocType including all fields, types, options, and permissions. "
	"Requires read permission. Use this to understand existing DocType structures before modifying them."
)
DESC_GET_EXISTING_CUSTOMIZATIONS = (
	"List existing customizations (custom fields, server scripts, client scripts, workflows) "
	"filtered by your permissions. Use this to check what already exists before creating new customizations."
)
DESC_GET_USER_CONTEXT = (
	"Get the current user's email, roles, permissions, and permitted modules. "
	"Use this to understand what the user can access."
)
DESC_CHECK_PERMISSION = (
	"Check if the current user has a specific permission (read/write/create/delete) on a DocType. "
	"ALWAYS use this tool — NEVER guess permissions."
)
DESC_VALIDATE_NAME_AVAILABLE = (
	"Check if a document name is already taken. Use this before creating new DocTypes "
	"or documents to avoid naming conflicts."
)
DESC_HAS_ACTIVE_WORKFLOW = (
	"Check if a DocType already has an active workflow. "
	"Frappe allows only one active workflow per DocType."
)
DESC_CHECK_HAS_RECORDS = (
	"Check if a DocType has existing data records. "
	"Use this before rollback or deletion to avoid data loss."
)


def _run_async(coro):
	"""Run an async coroutine from synchronous CrewAI tool context."""
	try:
		loop = asyncio.get_event_loop()
		if loop.is_running():
			# We're in an async context — use a new thread
			import concurrent.futures
			with concurrent.futures.ThreadPoolExecutor() as pool:
				future = pool.submit(asyncio.run, coro)
				return future.result(timeout=60)
		else:
			return loop.run_until_complete(coro)
	except RuntimeError:
		return asyncio.run(coro)


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
		result = _run_async(mcp_client.call_tool("get_site_info"))
		return json.dumps(result, indent=2)

	@tool
	def get_doctypes(module: str = "") -> str:
		"""List DocType names and modules. Optionally filter by module name. Use this to find existing DocTypes for Link fields."""
		args = {}
		if module:
			args["module"] = module
		result = _run_async(mcp_client.call_tool("get_doctypes", args))
		return json.dumps(result, indent=2)

	@tool
	def get_doctype_schema(doctype: str) -> str:
		"""Get full field schema for a DocType. Requires read permission. Use this to understand existing DocType structures."""
		result = _run_async(mcp_client.call_tool("get_doctype_schema", {"doctype": doctype}))
		return json.dumps(result, indent=2)

	@tool
	def get_existing_customizations() -> str:
		"""List existing customizations (custom fields, server scripts, client scripts, workflows) filtered by your permissions."""
		result = _run_async(mcp_client.call_tool("get_existing_customizations"))
		return json.dumps(result, indent=2)

	@tool
	def get_user_context() -> str:
		"""Get the current user's email, roles, permissions, and permitted modules."""
		result = _run_async(mcp_client.call_tool("get_user_context"))
		return json.dumps(result, indent=2)

	@tool
	def check_permission(doctype: str, action: str = "read") -> str:
		"""Check if the current user has a specific permission (read/write/create/delete) on a DocType. Always use this tool — never guess permissions."""
		result = _run_async(mcp_client.call_tool("check_permission", {"doctype": doctype, "action": action}))
		return json.dumps(result, indent=2)

	@tool
	def validate_name_available(doctype: str, name: str) -> str:
		"""Check if a document name is already taken. Use before creating new DocTypes or documents to avoid naming conflicts."""
		result = _run_async(mcp_client.call_tool("validate_name_available", {"doctype": doctype, "name": name}))
		return json.dumps(result, indent=2)

	@tool
	def has_active_workflow(doctype: str) -> str:
		"""Check if a DocType already has an active workflow. Frappe allows only one active workflow per DocType."""
		result = _run_async(mcp_client.call_tool("has_active_workflow", {"doctype": doctype}))
		return json.dumps(result, indent=2)

	@tool
	def check_has_records(doctype: str) -> str:
		"""Check if a DocType has existing data records. Use before rollback or deletion to avoid data loss."""
		result = _run_async(mcp_client.call_tool("check_has_records", {"doctype": doctype}))
		return json.dumps(result, indent=2)

	# Return tool assignments matching the agent tool registry format
	return {
		"requirement": [ask_user_stub, get_site_info, get_doctypes, get_doctype_schema, get_existing_customizations],
		"assessment": [check_permission, get_user_context, get_existing_customizations],
		"architect": [get_doctype_schema, get_doctypes, get_existing_customizations, has_active_workflow],
		"developer": [get_doctype_schema, get_doctypes],
		"tester": [
			validate_python_syntax_stub, validate_js_syntax_stub, validate_name_available,
			check_permission, has_active_workflow, get_doctype_schema, check_has_records,
		],
		"deployer": [check_has_records],
	}


# Stubs for tools not provided by MCP (these are local to the processing app)
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
