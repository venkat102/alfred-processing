"""Insights mode handler - read-only Q&A with MCP tools, no changeset.

Phase B of the three-mode chat feature. Builds a single-agent CrewAI crew
with read-only MCP tools and runs it to produce a markdown answer about
the user's Frappe site state.

The handler reuses as much of the existing crew infrastructure as possible:
  - `build_insights_crew` (defined next to `build_lite_crew` in crew.py)
  - `run_crew` for execution, event streaming, and error boundaries
  - `init_run_state` for the per-run MCP tool budget cap
  - `build_mcp_tools(...)["insights"]` for the read-only tool subset

Key differences from dev mode:
  - Tool budget is 5 (vs 30 in dev) - Insights should be fast and cheap.
    If the agent can't answer in 5 tool calls, it says what it found.
  - Output is a markdown string, not a changeset. The pipeline emits it
    as `insights_reply` message type.
  - No dry-run, no reflection, no approval, no DB writes.

What this handler does NOT do:
  - Persist conversation memory. That happens in the pipeline after the
    handler returns so the orchestrator short-circuit code stays in one
    place.
  - Emit WebSocket messages directly. The pipeline emits `insights_reply`
    after getting the string back.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from alfred.models.insights_result import InsightsResult

if TYPE_CHECKING:
	from alfred.api.websocket import ConnectionState

logger = logging.getLogger("alfred.handlers.insights")

# Phase B: small budget. Insights should be cheap and fast.
# If the agent needs more than 5 tool calls, it should give a partial
# answer and let the user follow up.
_INSIGHTS_TOOL_BUDGET = 5


async def handle_insights(
	prompt: str,
	conn: ConnectionState,
	conversation_id: str,
	user_context: dict,
	event_callback=None,
) -> InsightsResult:
	"""Run the Insights crew and return a structured result.

	When ``ALFRED_REPORT_HANDOFF=1`` is set and the prompt is report-shaped
	(tabular, filterable, aggregation-ready), the returned ``InsightsResult``
	carries a ``report_candidate`` the client can use to offer a "Save as
	Report" handoff. Otherwise ``report_candidate`` is None.

	Args:
		prompt: The user's raw question.
		conn: Active WebSocket connection (for MCP client + site_config).
		conversation_id: Conversation id (threaded into run state + events).
		user_context: Dict with user, roles, site_id.
		event_callback: Optional async (event_type, data) callback. Passed
			through to `run_crew` so the UI sees agent_started / task_started /
			crew_completed events the same way dev mode does.

	Returns:
		An ``InsightsResult`` with ``reply`` (markdown) and optional
		``report_candidate``. Never raises - on any failure the reply is
		a best-effort fallback message and ``report_candidate`` is None.
	"""
	from alfred.agents.crew import build_insights_crew, run_crew
	from alfred.tools.mcp_tools import build_mcp_tools, init_run_state

	# Build read-only tool set from the live MCP client.
	if conn.mcp_client is None:
		logger.warning(
			"Insights handler called with no MCP client - falling back to empty tool set"
		)
		insights_tools = []
	else:
		all_tools = build_mcp_tools(conn.mcp_client)
		insights_tools = all_tools.get("insights", [])
		# Reset per-run tracking with a tight budget so read-only Q&A can't
		# accidentally fan out into 20 tool calls.
		init_run_state(
			conn.mcp_client,
			conversation_id=conversation_id,
			budget=_INSIGHTS_TOOL_BUDGET,
		)

	if not insights_tools:
		logger.warning(
			"Insights handler has no tools - the agent will reply from LLM "
			"knowledge only, which defeats the point of Insights mode"
		)

	try:
		crew, state = build_insights_crew(
			user_prompt=prompt,
			user_context=user_context,
			site_config=conn.site_config or {},
			insights_tools=insights_tools,
		)
	except Exception as e:  # noqa: BLE001 — CrewAI builder boundary; internal raises (LLM init, tool wiring, agent factory) are 3rd-party and we must degrade rather than crash the chat
		logger.warning("Failed to build insights crew: %s", e, exc_info=True)
		return InsightsResult(reply=(
			"I wasn't able to spin up the Insights agent just now. "
			"Try again in a moment, or rephrase your question."
		))

	try:
		result = await run_crew(
			crew=crew,
			state=state,
			store=None,
			site_id=conn.site_id,
			conversation_id=conversation_id,
			event_callback=event_callback,
		)
	except Exception as e:  # noqa: BLE001 — CrewAI run_crew boundary; LLM / tool / Redis / crewai-internal raises must degrade to a user-visible apology rather than propagate
		logger.warning("Insights crew run raised: %s", e, exc_info=True)
		return InsightsResult(reply=(
			"I hit an error while looking that up on your site. "
			"Try again in a moment."
		))

	if not isinstance(result, dict) or result.get("status") != "completed":
		err = (result or {}).get("error") if isinstance(result, dict) else None
		logger.warning("Insights crew did not complete cleanly: %s", err)
		return InsightsResult(reply=(
			"I couldn't gather the site information to answer that. "
			+ (f"Details: {err}" if err else "Try again in a moment.")
		))

	raw = (result.get("result") or "").strip()
	if not raw:
		return InsightsResult(reply=(
			"I didn't get a useful answer back from the Insights agent. "
			"Try rephrasing your question - e.g. 'what DocTypes do I have "
			"in the Selling module?'."
		))

	# Strip a leading code fence if the model wrapped the whole answer in
	# ```markdown blocks. Markdown renders fine inside code fences but the
	# UI looks cleaner without them.
	cleaned = raw
	if cleaned.startswith("```"):
		lines = cleaned.splitlines()
		if lines and lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		cleaned = "\n".join(lines).strip()

	reply = cleaned or raw

	# Attempt report_candidate extraction - gated by ALFRED_REPORT_HANDOFF
	# so pre-feature sites are unaffected. Extractor returns None when the
	# prompt isn't report-shaped (scalar / metadata / no target DocType).
	report_candidate = None
	from alfred.config import get_settings
	if get_settings().ALFRED_REPORT_HANDOFF:
		from alfred.handlers.insights_candidate import extract_report_candidate
		try:
			report_candidate = extract_report_candidate(prompt=prompt, reply=reply)
		except (ValueError, KeyError, AttributeError, OSError) as e:
			# extract_report_candidate uses regex (ValueError on bad
			# pattern at runtime), dict access (KeyError on a malformed
			# registry hit), and ModuleRegistry file load (OSError).
			# Anything else is a logic bug — let it surface.
			logger.warning(
				"report_candidate extraction failed: %s", e, exc_info=True,
			)

	return InsightsResult(reply=reply, report_candidate=report_candidate)
