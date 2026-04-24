"""Plan mode handler - produces a user-facing plan document, no crew.

Phase C of the three-mode chat feature. Builds the 3-agent plan crew
(Requirement Analyst, Feasibility Assessor, Solution Architect) and
runs it to produce a structured plan document. The user reviews the
plan in the chat UI and can either refine it (send another prompt) or
approve it (next turn runs Dev mode with the plan as the spec).

Design notes:
  - Uses the existing `run_crew` machinery for execution, event
    streaming, and error boundaries - same as dev and insights modes.
  - Tool budget is 15 (higher than insights at 5, lower than dev at 30).
    Plan mode needs enough tool calls to verify doctype schemas and
    patterns, but not so many that it starts wandering.
  - The final task outputs a JSON object. This handler parses it against
    the `PlanDoc` Pydantic model and falls back to a stub plan if
    validation fails - never raises to the pipeline.
  - The handler does NOT persist the plan to conversation memory. That's
    done by `_run_plan_short_circuit` in the pipeline so the handler
    stays testable without a Redis fixture.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
	from alfred.api.websocket import ConnectionState

logger = logging.getLogger("alfred.handlers.plan")

# Plan mode gets a moderate tool budget. The 3-agent crew needs enough
# calls to verify doctypes + patterns across Requirement / Assessment /
# Architect, but we cap it well below the dev-mode budget of 30 so a
# runaway plan can't burn the per-run budget.
_PLAN_TOOL_BUDGET = 15


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")


def _strip_code_fences(text: str) -> str:
	"""Strip leading/trailing ``` fences if the LLM wrapped its answer."""
	if not text:
		return text
	cleaned = text.strip()
	if cleaned.startswith("```"):
		lines = cleaned.splitlines()
		if lines and lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		cleaned = "\n".join(lines).strip()
	return cleaned


def _parse_plan_doc_json(raw: str) -> dict | None:
	"""Extract the first well-formed JSON object from crew output.

	The `generate_plan_doc` task is prompted to output ONLY a JSON object,
	but local models sometimes wrap it in fences or preface it with prose.
	Strip fences, try direct parse, fall back to the first balanced
	object-like substring.
	"""
	if not raw:
		return None

	cleaned = _strip_code_fences(raw)

	try:
		parsed = json.loads(cleaned)
		if isinstance(parsed, dict):
			return parsed
	except json.JSONDecodeError:
		# Local model wrapped JSON in prose — fall through to the
		# balanced-block scan below.
		pass

	# Fallback: find the first balanced { ... } block via JSONDecoder.
	decoder = json.JSONDecoder()
	for idx, ch in enumerate(cleaned):
		if ch != "{":
			continue
		try:
			parsed, _end = decoder.raw_decode(cleaned[idx:])
			if isinstance(parsed, dict):
				return parsed
		except json.JSONDecodeError:
			# This `{` didn't open a valid JSON object — keep scanning.
			continue
	return None


def _validate_as_plan_doc(raw_obj: dict, user_prompt: str) -> dict:
	"""Coerce a parsed JSON object into a PlanDoc-shaped dict.

	Falls back to the stub plan on validation errors. Never raises so the
	pipeline can always emit *something* to the user.
	"""
	from alfred.models.plan_doc import PlanDoc

	try:
		plan = PlanDoc.model_validate(raw_obj)
		return plan.model_dump()
	except ValidationError as e:
		# Pydantic schema mismatch — fall back to the stub plan rather
		# than surface a 30-line traceback to the user. Other Exception
		# types here would be a logic bug; let them propagate.
		logger.warning(
			"Plan doc validation failed: %s. Raw keys=%s",
			e,
			list(raw_obj.keys()) if isinstance(raw_obj, dict) else "(not a dict)",
		)
		stub = PlanDoc.stub(
			title="Plan could not be parsed",
			summary=(
				"The planning agent produced output but it didn't match the "
				"expected shape. Try rephrasing your request, or switch to "
				"Dev mode if you want to go straight to a build."
			),
		)
		# Keep whatever we can salvage from the raw output in open_questions
		# so the user has SOMETHING to look at.
		if isinstance(raw_obj, dict):
			preview = json.dumps(raw_obj)[:400]
			if preview:
				stub.open_questions = [
					f"Raw agent output (truncated): {preview}"
				]
		return stub.model_dump()


async def handle_plan(
	prompt: str,
	conn: "ConnectionState",
	conversation_id: str,
	user_context: dict,
	event_callback=None,
) -> dict:
	"""Run the Plan crew and return a PlanDoc-shaped dict.

	Args:
		prompt: The user's raw message. For Plan mode this is typically a
			design question like *"how would we approach adding approval
			to Expense Claims?"*.
		conn: Active WebSocket connection (for MCP client + site_config).
		conversation_id: Conversation id (threaded into run state + events).
		user_context: Dict with user, roles, site_id.
		event_callback: Optional async (event_type, data) callback. Forwarded
			to `run_crew` so the UI sees crew_started / task_started events
			during the plan run.

	Returns:
		A PlanDoc-shaped dict (validated against the Pydantic model). Never
		raises - on failure returns a stub plan explaining what went wrong.
	"""
	from alfred.agents.crew import run_crew
	from alfred.agents.plan_crew import build_plan_crew
	from alfred.tools.mcp_tools import build_mcp_tools, init_run_state

	# Build MCP tools restricted to the planning roles. We reuse the full
	# build_mcp_tools() output and drop the developer/tester/deployer keys;
	# the plan crew only assigns tools to requirement/assessment/architect.
	if conn.mcp_client is None:
		logger.warning(
			"Plan handler called with no MCP client - the planning agents "
			"will run without tool access, which will produce a vague plan"
		)
		custom_tools: dict | None = None
	else:
		custom_tools = build_mcp_tools(conn.mcp_client)
		# Budget cap. Plan mode gets more than insights (5) but less than
		# dev (30) - enough for the 3 agents to verify doctypes + patterns.
		init_run_state(
			conn.mcp_client,
			conversation_id=conversation_id,
			budget=_PLAN_TOOL_BUDGET,
		)

	from alfred.models.plan_doc import PlanDoc

	try:
		crew, state = build_plan_crew(
			user_prompt=prompt,
			user_context=user_context,
			site_config=conn.site_config or {},
			custom_tools=custom_tools,
		)
	except Exception as e:  # noqa: BLE001 — CrewAI builder boundary; same as handlers/insights.py for build_insights_crew. 3rd-party agent factory; degrade to stub plan.
		logger.warning("Failed to build plan crew: %s", e, exc_info=True)
		return PlanDoc.stub(
			title="Plan unavailable",
			summary=(
				"I couldn't spin up the planning crew just now. "
				"Try again in a moment."
			),
		).model_dump()

	try:
		result = await run_crew(
			crew=crew,
			state=state,
			store=None,
			site_id=conn.site_id,
			conversation_id=conversation_id,
			event_callback=event_callback,
		)
	except Exception as e:  # noqa: BLE001 — CrewAI run_crew boundary; same as handlers/insights.py. LLM/tool/Redis raises must degrade to a stub plan rather than crash the chat.
		logger.warning("Plan crew run raised: %s", e, exc_info=True)
		return PlanDoc.stub(
			title="Plan crew failed",
			summary=(
				"The planning agents hit an error partway through. "
				f"Try again in a moment. Details: {e}"
			),
		).model_dump()

	if not isinstance(result, dict) or result.get("status") != "completed":
		err = (result or {}).get("error") if isinstance(result, dict) else None
		logger.warning("Plan crew did not complete cleanly: %s", err)
		return PlanDoc.stub(
			title="Plan incomplete",
			summary=(
				"The planning crew didn't produce a usable plan. "
				+ (f"Details: {err}" if err else "Try rephrasing your request.")
			),
		).model_dump()

	raw_text = (result.get("result") or "").strip()
	parsed = _parse_plan_doc_json(raw_text)
	if parsed is None:
		logger.warning(
			"Plan crew output could not be parsed as JSON. First 300 chars: %r",
			raw_text[:300],
		)
		return PlanDoc.stub(
			title="Plan output unreadable",
			summary=(
				"The planning agents produced output but I couldn't parse "
				"it as JSON. Try rephrasing your request."
			),
		).model_dump()

	return _validate_as_plan_doc(parsed, user_prompt=prompt)
