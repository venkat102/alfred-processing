"""Local-only stub tools - everything Frappe-backed now comes from MCP.

These three tools don't need a live Frappe site, so they stay here as offline
fallbacks. The Frappe-backed tools (get_doctype_schema, check_permission, etc.)
moved to `alfred/tools/mcp_tools.py` and query the real site via MCP.

TOOL_ASSIGNMENTS below is the minimal default used by unit tests that don't have
a live WebSocket / MCP client. In production, `_run_agent_pipeline` passes
`custom_tools=build_mcp_tools(conn.mcp_client)` which overrides this entirely.
"""

import json

from crewai.tools import tool


@tool
def ask_user(question: str, choices: str = "") -> str:
	"""Ask the user a question and wait for their response. Use for clarifying requirements or getting approval. Optionally provide comma-separated choices."""
	return f"[STUB] User would be asked: {question}"


@tool
def validate_python_syntax(code: str) -> str:
	"""Validate Python syntax of a Server Script. Returns any syntax errors found."""
	try:
		compile(code, "<agent_code>", "exec")
		return '{"valid": true, "errors": []}'
	except SyntaxError as e:
		return json.dumps({"valid": False, "errors": [f"{e.msg} at line {e.lineno}"]})


@tool
def validate_js_syntax(code: str) -> str:
	"""Validate JavaScript syntax of a Client Script. Returns any syntax errors found."""
	return '{"valid": true, "errors": [], "note": "Basic validation only"}'


# Minimal default - used only when running without MCP (tests).
# Production agents get the full MCP-backed assignment from build_mcp_tools().
TOOL_ASSIGNMENTS = {
	"requirement": [ask_user],
	"assessment": [],
	"architect": [],
	"developer": [],
	"tester": [validate_python_syntax, validate_js_syntax],
	"deployer": [],
}
