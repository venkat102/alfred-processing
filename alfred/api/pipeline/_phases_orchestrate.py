"""Orchestrate-phase mixin: mode classification + short-circuits.

TD-H2 PR 3 split from ``alfred/api/pipeline.py``. Mixed into
``AgentPipeline`` via ``runner.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import WebSocketDisconnect

if TYPE_CHECKING:
	from alfred.api.pipeline.context import PipelineContext

logger = logging.getLogger("alfred.pipeline")


class _PhasesOrchestrateMixin:
	"""Mode-orchestration phases — runs the dev/plan/insights/chat dispatcher and short-circuits chat/insights/plan modes."""

	# Set on the concrete AgentPipeline class via the runner.
	ctx: PipelineContext

	async def _phase_orchestrate(self) -> None:
		"""Classify the prompt into a mode (dev/plan/insights/chat).

		Gated by ALFRED_ORCHESTRATOR_ENABLED (parsed by orchestrator.is_enabled).
		When the flag is off the pipeline behaves exactly as before (mode stays
		"dev", no LLM call, no skip). Chat mode short-circuits: the handler
		runs inline, emits a chat_reply message, and the pipeline stops via
		ctx.should_stop.
		"""
		from alfred.orchestrator import classify_mode, is_enabled

		ctx = self.ctx

		if not is_enabled():
			# Feature flag off - preserve pre-feature behavior.
			ctx.mode = "dev"
			return

		decision = await classify_mode(
			prompt=ctx.prompt,
			memory=ctx.conversation_memory,
			manual_override=ctx.manual_mode_override,
			site_config=ctx.conn.site_config or {},
			force_dev_override=ctx.force_dev_override,
		)
		ctx.mode = decision.mode
		ctx.orchestrator_reason = decision.reason
		ctx.orchestrator_source = decision.source

		logger.info(
			"Orchestrator decision for conversation=%s: mode=%s source=%s "
			"confidence=%s reason=%r",
			ctx.conversation_id, decision.mode, decision.source,
			decision.confidence, decision.reason,
		)

		# Emit a small status notice so the UI can render a mode badge /
		# auto-switch notification. Best-effort: don't crash the pipeline
		# if the WebSocket send fails.
		try:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "mode_switch",
				"data": {
					"conversation": ctx.conversation_id,
					"mode": decision.mode,
					"reason": decision.reason,
					"source": decision.source,
					"confidence": decision.confidence,
				},
			})
		except (RuntimeError, WebSocketDisconnect, OSError) as e:
			# WS send pattern: closed / disconnected / socket error.
			# Orchestrator decision is already recorded on ctx; UI
			# will catch up from the regular agent_status stream.
			logger.warning("mode_switch send failed: %s", e)

		# Phase A: chat mode short-circuits here.
		# Phase B: insights mode short-circuits here too.
		# Phase C: plan mode short-circuits here too (but runs a 3-agent crew
		# internally). Dev mode continues through enhance/clarify/build/run/post.
		if ctx.mode == "chat":
			await self._run_chat_short_circuit()
			ctx.should_stop = True
		elif ctx.mode == "insights":
			await self._run_insights_short_circuit()
			ctx.should_stop = True
		elif ctx.mode == "plan":
			await self._run_plan_short_circuit()
			ctx.should_stop = True

	async def _run_chat_short_circuit(self) -> None:
		"""Run the chat handler inline and emit a chat_reply message.

		Separated from _phase_orchestrate for testability - tests can mock
		out the chat handler without patching the orchestrator decision.
		"""
		from alfred.handlers.chat import handle_chat

		ctx = self.ctx

		try:
			reply = await handle_chat(
				prompt=ctx.prompt,
				memory=ctx.conversation_memory,
				user_context=ctx.user_context,
				site_config=ctx.conn.site_config or {},
			)
		except Exception as e:  # noqa: BLE001 — chat-handler boundary; handle_chat catches its own LLM failures and always returns a string, but a logic bug (new memory subclass misbehaving etc.) must not block the chat reply
			logger.warning("Chat handler raised: %s", e, exc_info=True)
			reply = (
				"Hi! I had trouble generating a reply just now. "
				"Try again in a moment, or send a build request."
			)

		ctx.chat_reply = reply

		try:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "chat_reply",
				"data": {
					"conversation": ctx.conversation_id,
					"reply": reply,
					"mode": "chat",
				},
			})
		except (RuntimeError, WebSocketDisconnect, OSError) as e:
			# WS send pattern; see _send_error for the same shape.
			logger.warning("chat_reply send failed: %s", e)

		# Persist conversation memory so follow-up turns see this exchange.
		# Master f8b0810: surfaces failure to user via info event rather
		# than silently log-and-continue.
		await self._save_memory_with_feedback()  # type: ignore[attr-defined]

	async def _run_insights_short_circuit(self) -> None:
		"""Run the insights handler inline and emit an insights_reply message.

		Insights mode spins up a single-agent crew with read-only MCP tools,
		runs it with a tight tool budget (5 calls), and emits the final
		markdown reply. Never produces a changeset, never writes to the DB.
		"""
		from alfred.handlers.insights import handle_insights

		ctx = self.ctx

		# Set up an event callback so the UI gets crew_started / task_started
		# events during the insights run, matching dev-mode behavior.
		async def _event_cb(event_type: str, data: dict) -> None:
			try:
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {"event": event_type, **data},
				})
			except (RuntimeError, WebSocketDisconnect, OSError) as e:
				# WS send pattern; see _send_error.
				logger.warning("insights event_callback send failed: %s", e)

		ctx.event_callback = _event_cb

		try:
			result = await handle_insights(
				prompt=ctx.prompt,
				conn=ctx.conn,
				conversation_id=ctx.conversation_id,
				user_context=ctx.user_context,
				event_callback=_event_cb,
			)
		except Exception as e:  # noqa: BLE001 — insights-handler boundary; handle_insights catches its own CrewAI/LLM failures and always returns an InsightsResult, but a logic bug must degrade to the canned reply rather than crash insights mode
			logger.warning("Insights handler raised: %s", e, exc_info=True)
			from alfred.models.insights_result import InsightsResult
			result = InsightsResult(reply=(
				"I had trouble looking that up on your site just now. "
				"Try again in a moment, or rephrase your question."
			))

		ctx.insights_reply = result.reply

		try:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "insights_reply",
				"data": {
					"conversation": ctx.conversation_id,
					"reply": result.reply,
					"mode": "insights",
					"report_candidate": (
						result.report_candidate.model_dump()
						if result.report_candidate else None
					),
				},
			})
		except (RuntimeError, WebSocketDisconnect, OSError) as e:
			# WS send pattern.
			logger.warning("insights_reply send failed: %s", e)

		# Record the Q/A pair in conversation memory so later Plan/Dev turns
		# can resolve references like "that workflow I asked about".
		if ctx.conversation_memory is not None:
			try:
				ctx.conversation_memory.add_insights_query(ctx.prompt, result.reply)
			except (AttributeError, TypeError, KeyError) as e:
				# Memory mutation failure: AttributeError on unexpected
				# memory shape, TypeError on bad argument types, KeyError
				# on missing internal fields.
				logger.warning("insights memory record failed: %s", e)

		# Master f8b0810: surface save failures to user via info event.
		await self._save_memory_with_feedback()  # type: ignore[attr-defined]

	# ── Plan -> Dev handoff helpers (Phase C) ───────────────────────

	_PLAN_APPROVAL_PATTERNS = (
		"approve and build the plan",
		"approve the plan",
		"build the plan",
		"build it",
		"go ahead with the plan",
		"yes, build it",
		"yes build it",
		"let's build it",
		"lets build it",
		"proceed with the plan",
	)

	def _maybe_approve_active_plan(self) -> None:
		"""Flip the active plan to `status="approved"` when the user signals
		approval in this turn.

		Called at the top of `_phase_enhance` (Dev mode only). The signal
		is either:
		  1. The prompt explicitly contains an approval phrase (button click
		     sends the canned "Approve and build the plan" prompt), or
		  2. An active plan is already `status="approved"` from a previous
		     handoff - we leave it alone so the enhancer keeps seeing it.

		Plans flagged as "built" are left alone (a prior Dev run already
		consumed them) and do NOT get re-injected.
		"""
		ctx = self.ctx
		memory = ctx.conversation_memory
		if memory is None or not memory.active_plan:
			return

		current_status = (memory.active_plan or {}).get("status") or "proposed"
		if current_status in ("built", "rejected"):
			return
		if current_status == "approved":
			# Already flipped in an earlier turn - leave it alone so the
			# enhancer keeps injecting it.
			return

		prompt_lower = (ctx.prompt or "").strip().lower()
		if not any(pat in prompt_lower for pat in self._PLAN_APPROVAL_PATTERNS):
			# No approval signal. Plan stays "proposed" and is rendered as
			# summary-only in the memory context block.
			return

		try:
			memory.mark_active_plan_status("approved")
			logger.info(
				"Plan -> Dev handoff: marked active_plan approved for "
				"conversation=%s (title=%r)",
				ctx.conversation_id,
				(memory.active_plan or {}).get("title"),
			)
		except (AttributeError, TypeError, KeyError) as e:
			# Memory mutation exception set; see insights handler for
			# the matching shape.
			logger.warning("Failed to mark active plan approved: %s", e)

	def _mark_active_plan_built_if_any(self) -> None:
		"""Flip an approved plan to `status="built"` after a Dev run completes.

		Prevents the plan from being re-injected on the NEXT Dev turn.
		"""
		ctx = self.ctx
		memory = ctx.conversation_memory
		if memory is None or not memory.active_plan:
			return
		if (memory.active_plan or {}).get("status") != "approved":
			return
		try:
			memory.mark_active_plan_status("built")
		except (AttributeError, TypeError, KeyError) as e:
			# Same memory-mutation shape as _maybe_approve_active_plan.
			logger.warning("Failed to mark active plan built: %s", e)

	async def _run_plan_short_circuit(self) -> None:
		"""Run the plan handler inline and emit a plan_doc message.

		Plan mode runs a 3-agent crew (Requirement, Assessment, Architect)
		that produces a structured plan doc instead of a changeset. The
		doc is recorded in ConversationMemory.active_plan + plan_documents
		so the next Dev turn can pick it up as a spec when the user
		approves it.
		"""
		from alfred.handlers.plan import handle_plan

		ctx = self.ctx

		# Same event callback pattern as insights - forward crew status
		# events to the UI so the user sees the Requirement / Assessment /
		# Architect agents tick through.
		async def _event_cb(event_type: str, data: dict) -> None:
			try:
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {"event": event_type, **data},
				})
			except (RuntimeError, WebSocketDisconnect, OSError) as e:
				# WS send pattern.
				logger.warning("plan event_callback send failed: %s", e)

		ctx.event_callback = _event_cb

		try:
			plan_dict = await handle_plan(
				prompt=ctx.prompt,
				conn=ctx.conn,
				conversation_id=ctx.conversation_id,
				user_context=ctx.user_context,
				event_callback=_event_cb,
			)
		except Exception as e:  # noqa: BLE001 — plan-handler boundary; handle_plan catches its own CrewAI/LLM failures and always returns a PlanDoc dict, but a logic bug must degrade to the stub rather than crash plan mode
			logger.warning("Plan handler raised: %s", e, exc_info=True)
			from alfred.models.plan_doc import PlanDoc

			plan_dict = PlanDoc.stub(
				title="Plan unavailable",
				summary=(
					"I hit an error while putting together a plan. "
					"Try again in a moment."
				),
			).model_dump()

		ctx.plan_doc = plan_dict

		try:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "plan_doc",
				"data": {
					"conversation": ctx.conversation_id,
					"plan": plan_dict,
					"mode": "plan",
				},
			})
		except (RuntimeError, WebSocketDisconnect, OSError) as e:
			# WS send pattern.
			logger.warning("plan_doc send failed: %s", e)

		# Record as a proposed plan. The user has NOT approved it yet -
		# they click "Approve & Build" in the UI to flip status=approved,
		# which in turn routes the next prompt to Dev mode with this plan
		# as the spec.
		if ctx.conversation_memory is not None:
			try:
				ctx.conversation_memory.add_plan_document(
					plan_dict, status="proposed"
				)
			except (AttributeError, TypeError, KeyError) as e:
				# Memory mutation shape.
				logger.warning("plan memory record failed: %s", e)

		# Master f8b0810: surface save failures to user via info event.
		await self._save_memory_with_feedback()  # type: ignore[attr-defined]

