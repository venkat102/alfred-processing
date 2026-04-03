"""Stub implementations of agent tools.

These stubs allow agents to be instantiated and tested without
a running MCP server or Client App. Real implementations come
from Tasks 2.3 (MCP Server) and 2.4 (MCP Client + CrewAI wrappers).
"""

from crewai.tools import tool


# ── Tier 1: Reference Tools ──────────────────────────────────────

@tool
def get_site_info() -> str:
	"""Get basic site information including Frappe version, installed apps, default company, and country."""
	return '{"frappe_version": "17.x.x-develop", "apps": ["frappe", "erpnext"], "company": "Example Inc", "country": "India"}'


@tool
def get_doctypes(module: str = "") -> str:
	"""List DocType names and modules. Optionally filter by module name. Use this to find existing DocTypes for Link fields."""
	return '[{"name": "ToDo", "module": "Desk"}, {"name": "User", "module": "Core"}]'


# ── Tier 2: Schema Tools ─────────────────────────────────────────

@tool
def get_doctype_schema(doctype: str) -> str:
	"""Get the full field schema for a DocType. Requires read permission. Use this to understand existing DocType structures before modifying them."""
	return f'{{"doctype": "{doctype}", "fields": [], "permissions": []}}'


@tool
def get_existing_customizations() -> str:
	"""List existing customizations (custom fields, server scripts, client scripts, workflows) filtered by your permissions."""
	return '{"custom_fields": [], "server_scripts": [], "client_scripts": [], "workflows": []}'


@tool
def get_user_context() -> str:
	"""Get the current user's email, roles, permissions, and permitted modules."""
	return '{"user": "Administrator", "roles": ["System Manager"], "modules": ["Core", "Desk"]}'


# ── Tier 3: Validation Tools ─────────────────────────────────────

@tool
def check_permission(doctype: str, action: str = "read") -> str:
	"""Check if the current user has a specific permission (read/write/create/delete) on a DocType. Always use this tool - never guess permissions."""
	return f'{{"doctype": "{doctype}", "action": "{action}", "permitted": true}}'


@tool
def validate_name_available(doctype: str, name: str) -> str:
	"""Check if a document name is already taken. Use this before creating new DocTypes or documents to avoid naming conflicts."""
	return f'{{"doctype": "{doctype}", "name": "{name}", "available": true}}'


@tool
def has_active_workflow(doctype: str) -> str:
	"""Check if a DocType already has an active workflow. Frappe allows only one active workflow per DocType."""
	return f'{{"doctype": "{doctype}", "has_active_workflow": false}}'


@tool
def check_has_records(doctype: str) -> str:
	"""Check if a DocType has existing data records. Use this before rollback or deletion to avoid data loss."""
	return f'{{"doctype": "{doctype}", "has_records": false, "count": 0}}'


# ── User Interaction Tool ────────────────────────────────────────

@tool
def ask_user(question: str, choices: str = "") -> str:
	"""Ask the user a question and wait for their response. Use for clarifying requirements or getting approval. Optionally provide comma-separated choices."""
	return f"[STUB] User would be asked: {question}"


# ── Syntax Validation Tools ──────────────────────────────────────

@tool
def validate_python_syntax(code: str) -> str:
	"""Validate Python syntax of a Server Script. Returns any syntax errors found."""
	try:
		compile(code, "<agent_code>", "exec")
		return '{"valid": true, "errors": []}'
	except SyntaxError as e:
		return f'{{"valid": false, "errors": ["{e.msg} at line {e.lineno}"]}}'


@tool
def validate_js_syntax(code: str) -> str:
	"""Validate JavaScript syntax of a Client Script. Returns any syntax errors found."""
	# Basic JS validation - full validation would use a JS parser
	return '{"valid": true, "errors": [], "note": "Basic validation only - full JS parsing not available in stub"}'


# ── Tool Registry ────────────────────────────────────────────────
# Maps agent names to their assigned tools (from the design document)

TOOL_ASSIGNMENTS = {
	"requirement": [ask_user, get_site_info, get_doctypes, get_doctype_schema, get_existing_customizations],
	"assessment": [check_permission, get_user_context, get_existing_customizations],
	"architect": [get_doctype_schema, get_doctypes, get_existing_customizations, has_active_workflow],
	"developer": [get_doctype_schema, get_doctypes],
	"tester": [
		validate_python_syntax, validate_js_syntax, validate_name_available,
		check_permission, has_active_workflow, get_doctype_schema, check_has_records,
	],
	"deployer": [check_has_records],
}
