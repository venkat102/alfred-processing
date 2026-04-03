"""Prompt injection defense - regex sanitizer + intent classifier.

Layer 1: Fast regex sanitizer catches known injection patterns.
Layer 2: LLM-based intent classifier categorizes the prompt.
Unknown intents are flagged for admin review.

All patterns are configurable - stored in site_config from Alfred Settings.
"""

import json
import logging
import re

logger = logging.getLogger("alfred.defense")

# ── Default Injection Patterns ────────────────────────────────────
# These are checked against user prompts before agent processing.
# Patterns are case-insensitive regex.

DEFAULT_INJECTION_PATTERNS = [
	(r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions?", "Instruction override attempt"),
	(r"disregard\s+(all\s+)?(previous|above|prior)", "Instruction override attempt"),
	(r"forget\s+(everything|all|your)\s+(instructions?|rules?|guidelines?)", "Instruction override attempt"),
	(r"you\s+are\s+now\s+(a|an)\s+", "Role hijack attempt"),
	(r"act\s+as\s+(if\s+)?(you\s+are|a|an)\s+", "Role hijack attempt"),
	(r"pretend\s+(you\s+are|to\s+be)", "Role hijack attempt"),
	(r"skip\s+(all\s+)?permission\s+checks?", "Permission bypass attempt"),
	(r"ignore\s+permissions?", "Permission bypass attempt"),
	(r"bypass\s+(security|permissions?|auth)", "Security bypass attempt"),
	(r"execute\s+(raw\s+)?sql", "SQL injection attempt"),
	(r"frappe\.db\.sql\s*\(", "Direct SQL call attempt"),
	(r"import\s+os\b", "System access attempt"),
	(r"import\s+subprocess", "System access attempt"),
	(r"__import__\s*\(", "Dynamic import attempt"),
	(r"eval\s*\(", "Code execution attempt"),
	(r"exec\s*\(", "Code execution attempt"),
	(r"system\s*\(", "System command attempt"),
	(r"rm\s+-rf", "Destructive command attempt"),
	(r"drop\s+table", "Database destruction attempt"),
	(r"delete\s+from\s+tab", "Direct database manipulation attempt"),
]

# ── Known Intent Categories ───────────────────────────────────────

KNOWN_INTENTS = [
	"create_doctype",
	"modify_doctype",
	"create_workflow",
	"create_report",
	"create_script",
	"create_notification",
	"create_print_format",
	"add_custom_field",
	"general_question",
]


def sanitize_prompt(prompt: str, custom_patterns: list | None = None) -> dict:
	"""Check a prompt against known injection patterns.

	Args:
		prompt: The user's input text.
		custom_patterns: Optional additional patterns from Alfred Settings.

	Returns:
		{"safe": bool, "threats": [{"pattern": str, "reason": str}]}
	"""
	threats = []
	patterns = DEFAULT_INJECTION_PATTERNS.copy()

	if custom_patterns:
		for p in custom_patterns:
			if isinstance(p, dict) and "pattern" in p and "reason" in p:
				patterns.append((p["pattern"], p["reason"]))
			elif isinstance(p, (list, tuple)) and len(p) >= 2:
				patterns.append((p[0], p[1]))

	for pattern, reason in patterns:
		try:
			if re.search(pattern, prompt, re.IGNORECASE):
				threats.append({"pattern": pattern, "reason": reason})
		except re.error:
			logger.warning("Invalid regex pattern in sanitizer: %s", pattern)

	if threats:
		logger.warning(
			"Prompt injection detected: %d threat(s) found - %s",
			len(threats),
			", ".join(t["reason"] for t in threats),
		)

	return {"safe": len(threats) == 0, "threats": threats}


def classify_intent(prompt: str) -> str:
	"""Classify a prompt's intent using keyword heuristics.

	For production, this would use a lightweight LLM call.
	For now, uses keyword matching as a fast approximation.

	Returns one of KNOWN_INTENTS or "unknown".
	"""
	prompt_lower = prompt.lower()

	# Keyword-based classification
	if any(kw in prompt_lower for kw in ["create a doctype", "new doctype", "make a doctype", "build a doctype"]):
		return "create_doctype"
	if any(kw in prompt_lower for kw in ["modify", "change", "update", "edit", "alter"]) and "doctype" in prompt_lower:
		return "modify_doctype"
	if any(kw in prompt_lower for kw in ["workflow", "approval", "approve", "state machine"]):
		return "create_workflow"
	if any(kw in prompt_lower for kw in ["report", "dashboard", "chart", "analytics"]):
		return "create_report"
	if any(kw in prompt_lower for kw in ["script", "server script", "client script", "automation"]):
		return "create_script"
	if any(kw in prompt_lower for kw in ["notification", "alert", "email notification", "notify"]):
		return "create_notification"
	if any(kw in prompt_lower for kw in ["print format", "print", "pdf"]):
		return "create_print_format"
	if any(kw in prompt_lower for kw in ["custom field", "add field", "new field"]):
		return "add_custom_field"
	if any(kw in prompt_lower for kw in ["how", "what", "why", "explain", "help", "can you", "is it possible"]):
		return "general_question"

	# Broad catch: if it mentions common Frappe terms, it's likely a valid request
	frappe_terms = ["doctype", "field", "form", "list", "permission", "role", "module"]
	if any(term in prompt_lower for term in frappe_terms):
		return "create_doctype"  # Default to most common intent

	return "unknown"


def check_prompt(prompt: str, custom_patterns: list | None = None) -> dict:
	"""Full prompt defense check: sanitize + classify.

	Returns:
		{
			"allowed": bool,
			"sanitizer": {"safe": bool, "threats": [...]},
			"intent": str,
			"needs_review": bool,
			"rejection_reason": str | None,
		}
	"""
	# Step 1: Fast sanitizer
	sanitizer_result = sanitize_prompt(prompt, custom_patterns)

	if not sanitizer_result["safe"]:
		reasons = ", ".join(t["reason"] for t in sanitizer_result["threats"])
		return {
			"allowed": False,
			"sanitizer": sanitizer_result,
			"intent": "blocked",
			"needs_review": False,
			"rejection_reason": f"Prompt blocked by security filter: {reasons}",
		}

	# Step 2: Intent classification
	intent = classify_intent(prompt)

	if intent == "unknown":
		return {
			"allowed": False,
			"sanitizer": sanitizer_result,
			"intent": intent,
			"needs_review": True,
			"rejection_reason": "Unable to classify prompt intent. Flagged for admin review.",
		}

	return {
		"allowed": True,
		"sanitizer": sanitizer_result,
		"intent": intent,
		"needs_review": False,
		"rejection_reason": None,
	}
