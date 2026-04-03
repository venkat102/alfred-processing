"""Code validation tools for the Tester Agent.

All validation is deterministic code - no LLM involvement.
Two phases: static checks (offline) and Frappe-specific checks.

Dry-run simulation (via MCP tools) is handled by the agent's
backstory directing it to call MCP tools, not by this module.
"""

import ast
import json
import re

from crewai.tools import tool

# ── Constants ─────────────────────────────────────────────────────

FORBIDDEN_IMPORTS = {"os", "sys", "subprocess", "shutil", "importlib", "socket", "http", "urllib"}
FORBIDDEN_FUNCTIONS = {"eval", "exec", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals"}
FORBIDDEN_PATTERNS = [
	(r"frappe\.db\.sql\s*\(", "frappe.db.sql() (raw SQL) - use Frappe ORM instead"),
	(r"(?<!\.)open\s*\(", "open() (file operations) - not allowed in Server Scripts"),
	(r"requests\.", "requests library - external HTTP calls not allowed in sandbox"),
]

VALID_FRAPPE_FIELD_TYPES = {
	"Autocomplete", "Attach", "Attach Image", "Barcode", "Button",
	"Check", "Code", "Color", "Column Break", "Currency", "Data",
	"Date", "Datetime", "Duration", "Dynamic Link", "Float",
	"Fold", "Geolocation", "Heading", "HTML", "HTML Editor", "Icon",
	"Image", "Int", "JSON", "Link", "Long Text", "Markdown Editor",
	"Password", "Percent", "Phone", "Read Only", "Rating",
	"Section Break", "Select", "Signature", "Small Text",
	"Tab Break", "Table", "Table MultiSelect", "Text",
	"Text Editor", "Time",
}

RESERVED_FIELDNAMES = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "parent", "parenttype", "parentfield",
	"doctype", "amended_from",
}


# ── Python Validation ─────────────────────────────────────────────

def validate_python_syntax(code: str) -> dict:
	"""Validate Python syntax and check for forbidden patterns.

	Returns:
		{"valid": bool, "errors": [{"type": str, "message": str, "line": int|None}]}
	"""
	errors = []

	# 1. Syntax check via ast
	try:
		tree = ast.parse(code)
	except SyntaxError as e:
		errors.append({
			"type": "syntax_error",
			"message": f"{e.msg}",
			"line": e.lineno,
		})
		return {"valid": False, "errors": errors}

	# 2. Check for forbidden imports
	for node in ast.walk(tree):
		if isinstance(node, (ast.Import, ast.ImportFrom)):
			if isinstance(node, ast.Import):
				for alias in node.names:
					module_root = alias.name.split(".")[0]
					if module_root in FORBIDDEN_IMPORTS:
						errors.append({
							"type": "forbidden_import",
							"message": f"Forbidden import: '{alias.name}' - not allowed in Server Script sandbox",
							"line": node.lineno,
						})
			elif isinstance(node, ast.ImportFrom) and node.module:
				module_root = node.module.split(".")[0]
				if module_root in FORBIDDEN_IMPORTS:
					errors.append({
						"type": "forbidden_import",
						"message": f"Forbidden import: 'from {node.module}' - not allowed in Server Script sandbox",
						"line": node.lineno,
					})

	# 3. Check for forbidden function calls
	for node in ast.walk(tree):
		if isinstance(node, ast.Call):
			func_name = ""
			if isinstance(node.func, ast.Name):
				func_name = node.func.id
			elif isinstance(node.func, ast.Attribute):
				func_name = node.func.attr

			if func_name in FORBIDDEN_FUNCTIONS:
				errors.append({
					"type": "forbidden_function",
					"message": f"Forbidden function: '{func_name}()' - not allowed in Server Script sandbox",
					"line": node.lineno,
				})

	# 4. Check for forbidden patterns via regex
	for pattern, description in FORBIDDEN_PATTERNS:
		for i, line in enumerate(code.split("\n"), 1):
			if re.search(pattern, line):
				errors.append({
					"type": "forbidden_pattern",
					"message": f"Forbidden: {description}",
					"line": i,
				})

	# 5. Check for missing permission checks (heuristic)
	has_data_access = bool(re.search(r"frappe\.(get_all|get_doc|get_list|db\.get_value|db\.get_all)", code))
	has_perm_check = bool(re.search(r"frappe\.(has_permission|only_for)", code))
	if has_data_access and not has_perm_check:
		errors.append({
			"type": "missing_permission_check",
			"message": "Server Script accesses data (frappe.get_all/get_doc) without a permission check (frappe.has_permission). Consider adding permission verification.",
			"line": None,
		})

	# 6. Check for hardcoded email addresses in sendmail
	if "frappe.sendmail" in code:
		email_pattern = re.compile(r'["\'][a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}["\']')
		for i, line in enumerate(code.split("\n"), 1):
			if "sendmail" in line and email_pattern.search(line):
				errors.append({
					"type": "hardcoded_email",
					"message": "Hardcoded email address in sendmail - use document context for recipients",
					"line": i,
				})

	return {"valid": len(errors) == 0, "errors": errors}


def validate_js_syntax(code: str) -> dict:
	"""Basic JavaScript syntax validation.

	Checks for common syntax issues without a full JS parser.
	"""
	errors = []

	# Check balanced brackets
	brackets = {"(": ")", "[": "]", "{": "}"}
	stack = []
	for i, char in enumerate(code):
		if char in brackets:
			stack.append((char, i))
		elif char in brackets.values():
			if not stack:
				errors.append({"type": "syntax_error", "message": f"Unmatched closing '{char}'", "line": code[:i].count("\n") + 1})
			else:
				open_char, _ = stack.pop()
				if brackets[open_char] != char:
					errors.append({"type": "syntax_error", "message": f"Mismatched brackets: '{open_char}' and '{char}'", "line": code[:i].count("\n") + 1})

	for open_char, pos in stack:
		errors.append({"type": "syntax_error", "message": f"Unclosed '{open_char}'", "line": code[:pos].count("\n") + 1})

	return {"valid": len(errors) == 0, "errors": errors}


# ── Frappe DocType Validation ─────────────────────────────────────

def validate_doctype_definition(doctype_data: dict) -> dict:
	"""Validate a Frappe DocType definition.

	Checks naming, field types, fieldname uniqueness, permissions, module.
	"""
	errors = []
	name = doctype_data.get("name", "")

	# Name validation
	if not name:
		errors.append({"type": "missing_name", "message": "DocType name is required"})
	elif not all(c.isalnum() or c == " " for c in name):
		errors.append({"type": "invalid_name", "message": f"DocType name '{name}' contains invalid characters"})

	# Module check
	module = doctype_data.get("module", "")
	if module != "Alfred":
		errors.append({"type": "wrong_module", "message": f"Module must be 'Alfred', got '{module}'"})

	# Fields validation
	fields = doctype_data.get("fields", [])
	fieldnames = set()
	for i, field in enumerate(fields):
		fn = field.get("fieldname", "")
		ft = field.get("fieldtype", "")

		# Check fieldname
		if fn and fn in RESERVED_FIELDNAMES:
			errors.append({"type": "reserved_fieldname", "message": f"Field '{fn}' uses a reserved name", "field_index": i})
		if fn and not re.match(r"^[a-z][a-z0-9_]*$", fn):
			errors.append({"type": "invalid_fieldname", "message": f"Fieldname '{fn}' must be snake_case (lowercase, underscores)", "field_index": i})
		if fn in fieldnames:
			errors.append({"type": "duplicate_fieldname", "message": f"Duplicate fieldname: '{fn}'", "field_index": i})
		fieldnames.add(fn)

		# Check field type
		if ft and ft not in VALID_FRAPPE_FIELD_TYPES:
			errors.append({"type": "invalid_fieldtype", "message": f"Invalid field type '{ft}' for field '{fn}'. Valid types: {', '.join(sorted(VALID_FRAPPE_FIELD_TYPES))}", "field_index": i})

		# Link fields must have options
		if ft == "Link" and not field.get("options"):
			errors.append({"type": "missing_link_target", "message": f"Link field '{fn}' must specify 'options' (target DocType)", "field_index": i})

		# Select fields must have options
		if ft == "Select" and not field.get("options"):
			errors.append({"type": "missing_select_options", "message": f"Select field '{fn}' must specify 'options' (newline-separated values)", "field_index": i})

		# Table fields must have options
		if ft == "Table" and not field.get("options"):
			errors.append({"type": "missing_table_target", "message": f"Table field '{fn}' must specify 'options' (child DocType)", "field_index": i})

	# Permission validation
	permissions = doctype_data.get("permissions", [])
	if not permissions:
		errors.append({"type": "no_permissions", "message": "DocType must have at least one permission rule"})

	for perm in permissions:
		role = perm.get("role", "")
		if role in ("Administrator", "All"):
			errors.append({"type": "dangerous_permission", "message": f"Permission grant to '{role}' role requires explicit user approval"})

	return {"valid": len(errors) == 0, "errors": errors, "doctype": name}


# ── Workflow Validation ───────────────────────────────────────────

def validate_workflow_definition(workflow_data: dict) -> dict:
	"""Validate a Frappe Workflow definition."""
	errors = []
	name = workflow_data.get("name", "")

	states = workflow_data.get("states", [])
	transitions = workflow_data.get("transitions", [])

	state_names = {s.get("state", "") for s in states}

	if not states:
		errors.append({"type": "no_states", "message": "Workflow must have at least one state"})

	# Check for initial state
	has_initial = any(s.get("doc_status") == "0" or s.get("state") == "Draft" for s in states)
	if not has_initial and states:
		errors.append({"type": "no_initial_state", "message": "Workflow should have an initial state (usually 'Draft' with doc_status=0)"})

	# Check transitions reference valid states
	for i, t in enumerate(transitions):
		source = t.get("state", "")
		target = t.get("next_state", "")
		if source and source not in state_names:
			errors.append({"type": "invalid_transition_source", "message": f"Transition {i}: source state '{source}' not in states list"})
		if target and target not in state_names:
			errors.append({"type": "invalid_transition_target", "message": f"Transition {i}: target state '{target}' not in states list"})

	# Check for orphan states (not reachable via any transition)
	reachable = set()
	for t in transitions:
		reachable.add(t.get("state", ""))
		reachable.add(t.get("next_state", ""))
	orphans = state_names - reachable
	if orphans and len(state_names) > 1:
		errors.append({"type": "orphan_states", "message": f"States not reachable via transitions: {', '.join(orphans)}"})

	return {"valid": len(errors) == 0, "errors": errors, "workflow": name}


# ── Changeset Order Validation ────────────────────────────────────

def validate_changeset_order(changeset: list[dict]) -> dict:
	"""Validate that changeset operations are in correct dependency order.

	If DocType B has a Link to DocType A, A must come before B.
	"""
	errors = []

	# Collect DocTypes being created and their order
	created_doctypes = {}
	for i, item in enumerate(changeset):
		if item.get("doctype") == "DocType" and item.get("operation") == "create":
			dt_name = item.get("data", {}).get("name", "")
			if dt_name:
				created_doctypes[dt_name] = i

	# Check Link field dependencies
	for i, item in enumerate(changeset):
		if item.get("doctype") == "DocType" and item.get("operation") == "create":
			fields = item.get("data", {}).get("fields", [])
			for field in fields:
				if field.get("fieldtype") == "Link":
					target = field.get("options", "")
					if target in created_doctypes and created_doctypes[target] > i:
						errors.append({
							"type": "dependency_order",
							"message": f"DocType '{item['data']['name']}' (order {i}) has Link to '{target}' (order {created_doctypes[target]}), but '{target}' must be created first",
						})

	# Check for circular dependencies
	# Simple cycle detection: if A links to B and B links to A
	links = {}
	for item in changeset:
		if item.get("doctype") == "DocType" and item.get("operation") == "create":
			dt_name = item.get("data", {}).get("name", "")
			fields = item.get("data", {}).get("fields", [])
			for field in fields:
				if field.get("fieldtype") == "Link" and field.get("options") in created_doctypes:
					links.setdefault(dt_name, set()).add(field["options"])

	for a, targets in links.items():
		for b in targets:
			if b in links and a in links.get(b, set()):
				errors.append({
					"type": "circular_dependency",
					"message": f"Circular dependency: '{a}' links to '{b}' and '{b}' links to '{a}'",
				})

	return {"valid": len(errors) == 0, "errors": errors}


# ── CrewAI Tool Wrappers ─────────────────────────────────────────

@tool
def validate_python_syntax_tool(code: str) -> str:
	"""Validate Python syntax of a Server Script. Checks for syntax errors, forbidden imports (os, sys, subprocess), forbidden functions (eval, exec), raw SQL usage, and missing permission checks."""
	result = validate_python_syntax(code)
	return json.dumps(result, indent=2)


@tool
def validate_js_syntax_tool(code: str) -> str:
	"""Validate JavaScript syntax of a Client Script. Checks for unmatched brackets and basic syntax issues."""
	result = validate_js_syntax(code)
	return json.dumps(result, indent=2)


@tool
def validate_doctype_tool(doctype_json: str) -> str:
	"""Validate a Frappe DocType definition. Checks naming conventions, field types, fieldname uniqueness, permissions, and module assignment."""
	try:
		data = json.loads(doctype_json) if isinstance(doctype_json, str) else doctype_json
	except json.JSONDecodeError as e:
		return json.dumps({"valid": False, "errors": [{"type": "json_error", "message": str(e)}]})
	result = validate_doctype_definition(data)
	return json.dumps(result, indent=2)


@tool
def validate_workflow_tool(workflow_json: str) -> str:
	"""Validate a Frappe Workflow definition. Checks states, transitions, initial state, and orphan states."""
	try:
		data = json.loads(workflow_json) if isinstance(workflow_json, str) else workflow_json
	except json.JSONDecodeError as e:
		return json.dumps({"valid": False, "errors": [{"type": "json_error", "message": str(e)}]})
	result = validate_workflow_definition(data)
	return json.dumps(result, indent=2)


@tool
def validate_changeset_order_tool(changeset_json: str) -> str:
	"""Validate that changeset operations are in correct dependency order. Checks that Link field targets are created before the linking DocType."""
	try:
		data = json.loads(changeset_json) if isinstance(changeset_json, str) else changeset_json
	except json.JSONDecodeError as e:
		return json.dumps({"valid": False, "errors": [{"type": "json_error", "message": str(e)}]})
	result = validate_changeset_order(data)
	return json.dumps(result, indent=2)
