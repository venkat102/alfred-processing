"""Deterministic permission matrix for the Assessment Agent.

This module contains ZERO LLM involvement - all permission decisions
are pure code based on a hardcoded mapping of customization types to
required Frappe roles.

Frappe uses additive permissions: if a user has System Manager among
their roles, they pass all checks regardless of other roles.
"""

import json

from crewai.tools import tool

# ── Permission Matrix ─────────────────────────────────────────────
# Maps customization type -> required Frappe role(s) (any one suffices)

PERMISSION_MATRIX: dict[str, list[str]] = {
	"DocType": ["System Manager"],
	"Custom Field": ["System Manager"],
	"Property Setter": ["System Manager"],
	"Server Script": ["System Manager"],
	"Client Script": ["System Manager"],
	"Workflow": ["Workflow Manager", "System Manager"],
	"Report": ["System Manager"],
	"Notification": ["System Manager"],
	"Print Format": ["System Manager"],
}

# ── Escalation Thresholds ────────────────────────────────────────

MAX_CHANGES_BEFORE_ESCALATION = 10

# Patterns that require human intervention (not doable via document customization)
ESCALATION_PATTERNS = [
	"bench command",
	"hooks.py",
	"app-level change",
	"core modification",
	"file system",
	"python file",
	"data migration",
	"database migration",
	"custom app",
]


def check_permissions(requirement_spec: dict, user_roles: list[str]) -> dict:
	"""Check if user's roles allow all customizations in the requirement spec.

	This function is 100% deterministic - same input always produces same output.
	No LLM, no randomness, no external API calls.

	Args:
		requirement_spec: Dict with 'customizations_needed' list.
		user_roles: List of Frappe role names the user has.

	Returns:
		{"passed": bool, "failed": [{"customization_type": ..., "required_role": ..., "reason": ...}]}
	"""
	role_set = set(user_roles)
	failed = []

	customizations = requirement_spec.get("customizations_needed", [])
	for item in customizations:
		cust_type = item.get("type", "Unknown")
		required_roles = PERMISSION_MATRIX.get(cust_type)

		if required_roles is None:
			# Unknown customization type - block by default
			failed.append({
				"customization_type": cust_type,
				"required_role": "Unknown",
				"permitted": False,
				"reason": f"Unrecognized customization type: '{cust_type}'. Cannot verify permissions.",
			})
			continue

		# Check if user has ANY of the required roles
		has_required = bool(role_set.intersection(set(required_roles)))
		if not has_required:
			failed.append({
				"customization_type": cust_type,
				"required_role": " or ".join(required_roles),
				"permitted": False,
				"reason": f"Requires role '{' or '.join(required_roles)}' but user has: {', '.join(sorted(role_set))}",
			})

	return {
		"passed": len(failed) == 0,
		"failed": failed,
	}


def assess_complexity(requirement_spec: dict) -> str:
	"""Assess the complexity of a requirement spec.

	Returns: "low" (1-2 changes), "medium" (3-5), "high" (6+)
	"""
	count = len(requirement_spec.get("customizations_needed", []))
	if count <= 2:
		return "low"
	elif count <= 5:
		return "medium"
	else:
		return "high"


def check_escalation_needed(requirement_spec: dict) -> str | None:
	"""Check if the requirement needs human escalation.

	Returns escalation reason or None if AI can handle it.
	"""
	customizations = requirement_spec.get("customizations_needed", [])

	# Check change count threshold
	if len(customizations) > MAX_CHANGES_BEFORE_ESCALATION:
		return f"Task involves {len(customizations)} changes (threshold: {MAX_CHANGES_BEFORE_ESCALATION}). Human review recommended."

	# Check for patterns requiring app-level changes
	spec_text = json.dumps(requirement_spec).lower()
	for pattern in ESCALATION_PATTERNS:
		if pattern in spec_text:
			return f"Requirement mentions '{pattern}' which requires app-level changes beyond document customization."

	# Check for unknown customization types
	for item in customizations:
		cust_type = item.get("type", "")
		if cust_type not in PERMISSION_MATRIX:
			return f"Unknown customization type '{cust_type}' - human review needed."

	return None


# ── CrewAI Tool Wrapper ──────────────────────────────────────────

@tool
def check_permissions_tool(requirement_spec_json: str, user_roles_json: str) -> str:
	"""Deterministic permission check for a requirement spec. Checks if the user's roles allow all requested customization types. ALWAYS use this tool - NEVER guess permissions.

	Args:
		requirement_spec_json: JSON string of the RequirementSpec.
		user_roles_json: JSON string of the user's role list.

	Returns:
		JSON result with passed/failed status and details.
	"""
	try:
		spec = json.loads(requirement_spec_json) if isinstance(requirement_spec_json, str) else requirement_spec_json
		roles = json.loads(user_roles_json) if isinstance(user_roles_json, str) else user_roles_json
	except json.JSONDecodeError as e:
		return json.dumps({"error": f"Invalid JSON input: {e}"})

	perm_result = check_permissions(spec, roles)
	complexity = assess_complexity(spec)
	escalation = check_escalation_needed(spec)

	result = {
		"permission_check": perm_result,
		"complexity": complexity,
		"escalation_needed": escalation is not None,
		"escalation_reason": escalation,
	}
	return json.dumps(result, indent=2)
