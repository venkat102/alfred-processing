"""Build-phase mixin: build_crew, run_crew, post_crew.

TD-H2 PR 3 split from ``alfred/api/pipeline.py``. Mixed into
``AgentPipeline`` via ``runner.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from fastapi import WebSocketDisconnect

from alfred.config import get_settings as _get_settings

if TYPE_CHECKING:
	from alfred.api.pipeline.context import PipelineContext

logger = logging.getLogger("alfred.pipeline")


class _PhasesBuildMixin:
	"""Crew lifecycle phases — build, run, and post-process."""

	# Set on the concrete AgentPipeline class via the runner.
	ctx: "PipelineContext"

	async def _phase_build_crew(self) -> None:
		"""Assemble the crew, MCP tool set, and per-run tracking state."""
		if self.ctx.mode != "dev":
			return

		from alfred.agents.crew import build_alfred_crew, build_lite_crew
		from alfred.tools.mcp_tools import build_mcp_tools

		ctx = self.ctx

		# Wipe stale crew state for this conversation id so we always start
		# fresh - CrewState resumption was built for crash recovery, not for
		# new prompts in an existing conversation.
		if ctx.store:
			try:
				await ctx.store.delete_task_state(
					ctx.conn.site_id, f"crew-state-{ctx.conversation_id}"
				)
			except (aioredis.RedisError, ValueError, TypeError) as e:
				# StateStore.delete_task_state is thin: JSON shape guard
				# (ValueError/TypeError) + Redis I/O. Anything else is a
				# logic bug and should surface, not get swallowed here.
				logger.debug("Failed to clear prior crew state (ignored): %s", e)

		ctx.custom_tools = (
			build_mcp_tools(ctx.conn.mcp_client) if ctx.conn.mcp_client else None
		)

		# Phase 1 per-run MCP tracking state (budget, dedup, failure counter).
		if (
			ctx.conn.mcp_client is not None
			and not _get_settings().ALFRED_PHASE1_DISABLED
		):
			from alfred.tools.mcp_tools import init_run_state
			init_run_state(ctx.conn.mcp_client, conversation_id=ctx.conversation_id)

		if ctx.pipeline_mode == "lite":
			lite_tools = (ctx.custom_tools or {}).get("lite", []) if ctx.custom_tools else []
			if not lite_tools:
				logger.warning(
					"Lite pipeline starting without MCP tools for %s - the agent "
					"will have no way to verify DocType schemas, check permissions, "
					"or run dry_run_changeset. Expect degraded output quality.",
					ctx.conn.site_id,
				)
			ctx.crew, ctx.crew_state = build_lite_crew(
				user_prompt=ctx.enhanced_prompt,
				user_context=ctx.user_context,
				site_config=ctx.conn.site_config,
				previous_state=None,
				lite_tools=lite_tools,
			)
		else:
			ctx.crew, ctx.crew_state = build_alfred_crew(
				user_prompt=ctx.enhanced_prompt,
				user_context=ctx.user_context,
				site_config=ctx.conn.site_config,
				previous_state=None,
				custom_tools=ctx.custom_tools,
				intent=ctx.intent,
				module_context=ctx.module_context,
			)

		# Set up the per-phase event callback now - the crew and all
		# post-crew phases share this one.
		async def _event_cb(event_type: str, data: dict) -> None:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "agent_status",
				"data": {"event": event_type, **data},
			})

		ctx.event_callback = _event_cb

	async def _phase_run_crew(self) -> None:
		if self.ctx.mode != "dev":
			return

		from alfred.agents.crew import run_crew

		ctx = self.ctx
		# _phase_build_crew populates both unconditionally when mode=dev;
		# run_crew is gated on that phase having run, so the pipeline
		# never reaches this point with crew_state=None. The asserts
		# make the guarantee explicit for type-checkers.
		assert ctx.crew is not None
		assert ctx.crew_state is not None
		timeout = ctx.conn.site_config.get("task_timeout_seconds", 300)
		ctx.crew_result = await asyncio.wait_for(
			run_crew(
				ctx.crew, ctx.crew_state, ctx.store,
				ctx.conn.site_id, ctx.conversation_id, ctx.event_callback,
			),
			timeout=timeout * 2,
		)

	async def _phase_post_crew(self) -> None:
		"""Extract changeset, rescue if needed, run safety nets, reflect,
		dry-run, send preview.

		TD-H1: the safety-net cluster (drift + rescue + backfill + report
		handoff + module validation + empty-changeset error) lives in
		``alfred.api.safety_nets`` so each concern is independently
		testable. Order is load-bearing — do not reshuffle without reading
		the concerns-list comment in docs/pending-tasks.md TD-H1.
		"""
		if self.ctx.mode != "dev":
			return

		from alfred.api.websocket import (
			_extract_changes,
			_dry_run_with_retry,
		)
		from alfred.agents.reflection import reflect_minimality
		from alfred.state.conversation_memory import save_conversation_memory
		from alfred.api.safety_nets import (
			apply_defaults_backfill,
			apply_module_validation,
			apply_report_handoff_safety_net,
			apply_rescue_if_empty,
			detect_drift_with_metric,
			emit_empty_changeset_error,
		)

		ctx = self.ctx
		result = ctx.crew_result or {}

		if result.get("status") != "completed":
			self._send_error_later(
				result.get("error", "Pipeline failed"),
				"PIPELINE_FAILED",
			)
			return

		ctx.result_text = result.get("result", "") or ""

		# Tight "crew completed" status. Do NOT leak result_text here —
		# if the crew drifted into prose, that string would show drift
		# to the user even when rescue later succeeds.
		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_status",
			"data": {
				"agent": "Orchestrator", "status": "completed",
				"message": "Crew finished. Validating and building changeset...",
			},
		})

		logger.info(
			"Pipeline result_text for conversation=%s (first 500): %r",
			ctx.conversation_id, ctx.result_text[:500],
		)

		# ── Safety-net chain ──────────────────────────────────
		drift_reason = detect_drift_with_metric(ctx)
		ctx.changes = _extract_changes(ctx.result_text) if not drift_reason else []
		await apply_rescue_if_empty(ctx, drift_reason)
		apply_defaults_backfill(ctx)
		apply_report_handoff_safety_net(ctx)
		await apply_module_validation(ctx)

		if emit_empty_changeset_error(self, drift_reason):
			return

		# Phase 3 #13 minimality reflection
		ctx.changes, ctx.removed_by_reflection = await reflect_minimality(
			ctx.prompt, ctx.changes, ctx.conn.site_config,
		)
		if ctx.removed_by_reflection and ctx.event_callback is not None:
			await ctx.event_callback("minimality_review", {
				"agent": "Reflection", "status": "pruned",
				"message": (
					f"Dropped {len(ctx.removed_by_reflection)} item(s) as not strictly needed: "
					+ "; ".join(
						f"{(r['item'] or {}).get('doctype', '?')} "
						f"'{(r['item'] or {}).get('data', {}).get('name', '?')}' "
						f"({r['reason']})"
						for r in ctx.removed_by_reflection
					)
				),
				"removed": [
					{
						"doctype": (r["item"] or {}).get("doctype"),
						"name": (r["item"] or {}).get("data", {}).get("name"),
						"reason": r["reason"],
					}
					for r in ctx.removed_by_reflection
				],
			})

		# Dry-run validation
		assert ctx.crew_state is not None  # build_crew ran before post_crew
		ctx.dry_run_result = await _dry_run_with_retry(
			ctx.conn, ctx.crew_state, ctx.changes,
			ctx.conn.site_config, ctx.event_callback,
		)
		ctx.changes = ctx.dry_run_result.pop("_final_changes", ctx.changes)

		# Persist conversation memory
		if ctx.conversation_memory is not None:
			ctx.conversation_memory.add_changeset_items(ctx.changes)
			# Phase C: if this Dev run consumed an approved plan, flip
			# the plan status to "built" so it doesn't get re-injected
			# on the next Dev turn.
			# Cross-mixin call — _mark_active_plan_built_if_any lives on
			# _PhasesOrchestrateMixin; resolved via AgentPipeline's MRO.
			self._mark_active_plan_built_if_any()  # type: ignore[attr-defined]
			await save_conversation_memory(
				ctx.store, ctx.conn.site_id, ctx.conversation_id, ctx.conversation_memory
			)

		# Send preview
		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "changeset",
			"data": {
				"conversation": ctx.conversation_id,
				"changes": ctx.changes,
				"result_text": ctx.result_text[:4000],
				"dry_run": ctx.dry_run_result,
				"module_validation_notes": ctx.module_validation_notes,
				"detected_module": ctx.module,
				"detected_module_secondaries": ctx.secondary_modules,
				"module_confidence": ctx.module_confidence,
			},
		})

	def _send_error_later(self, error: str, code: str, **extra: Any) -> None:
		"""Mark the context as stopped so `run()` emits the error on exit.

		Used from `_phase_post_crew` when the crew completed but produced
		no usable changeset - we want the same error-send shape the outer
		try/except gives us, so we route through the stop signal.
		"""
		self.ctx.stop(error=error, code=code, **extra)
