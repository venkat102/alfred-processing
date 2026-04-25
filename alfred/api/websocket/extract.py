"""Pure-function helpers for the WebSocket handler (TD-H2 split from
``alfred/api/websocket.py``).

Nothing here touches a live WebSocket / FastAPI request — these are the
parsers and shape-validators that the main handler calls to turn agent
output into a changeset and MCP tool names into a human-readable
activity string. Exercised directly in ``tests/test_websocket_helpers.py``.
"""

from __future__ import annotations

import ast
import json
import logging
import re

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

