"""Agent pipeline orchestrator - Phase 3 #12.

Wraps the previously-monolithic `_run_agent_pipeline` as an explicit linear
state machine over a shared `PipelineContext`. Each phase is a named method
that reads + mutates the context; the orchestrator iterates them in order
and auto-wraps each in a tracer span.

Why this shape:
  - Each phase is independently testable. Future unit tests can build a
    `PipelineContext` directly, call one phase method, and assert on the
    resulting state without booting the whole pipeline.
  - Adding a new phase is two small edits: add the method, add it to the
    `PHASES` list. No surgery in the middle of a 400-line function.
  - Observability is free - one `async with tracer.span(f"pipeline.{name}")`
    in `run()` covers every phase automatically.
  - Error boundaries are centralized: `run()` catches TimeoutError / generic
    exceptions once and emits the same user-visible error shape the old
    code did, regardless of which phase failed.

Deliberate non-goals:
  - True graph-based state machine (nodes + conditional edges). The current
    pipeline is fundamentally linear with a few early-exit conditions, and
    a graph would be over-engineering. Phases abort by setting
    `ctx.should_stop = True` with an error payload.
  - Replacing CrewAI. The `run_crew` phase still calls CrewAI directly -
    this orchestrator sits ABOVE CrewAI, not around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import re as _re

from alfred.obs import tracer

if TYPE_CHECKING:
	from alfred.api.websocket import ConnectionState
	from alfred.agents.crew import CrewState
	from alfred.state.store import StateStore
	from alfred.state.conversation_memory import ConversationMemory

logger = logging.getLogger("alfred.pipeline")


# ── Drift detection (training-data bleed) ─────────────────────────────
#
# qwen2.5-coder:32b on Ollama sometimes slips out of the task structure
# when the prompt exceeds its effective attention budget and falls back
# to its training-data prior for Frappe. The most common drift is a
# verbose documentation dump of Sales Order (the most-cited DocType in
# its training corpus), delivered as prose with no JSON. When that
# happens we want to detect it BEFORE feeding the prose into extraction
# / rescue / the UI, so the user sees a specific, actionable error
# instead of a confusing wall of off-topic text.
#
# Signals of drift:
#   1. Output mentions a DocType (Title-Cased multi-word token) that
#      does NOT appear in the user's prompt.
#   2. Output uses "documentation mode" giveaway phrases like "The
#      provided JSON structure" or "describes the metadata".
#   3. Output is long prose with no JSON brackets at all.
#   4. Output contains ERPNext-specific field names the user never
#      mentioned (customer_name, taxes_and_charges, sales_team, etc.).
#
# Any one signal is enough to flag drift. We err on the side of false
# negatives - we don't want to flag a legit answer that happens to
# mention a related DocType. That's why we require the "foreign
# doctype" check to be combined with at least one doc-mode giveaway.

_DOCUMENTATION_MODE_PHRASES = (
	"the provided json structure",
	"the provided json",
	"describes the metadata",
	"here's a breakdown",
	"here is a breakdown",
	"the following json",
	"this json object",
	"document type:",  # markdown heading from doc-mode dumps
	"example usage",
)

# Field names that appear in ERPNext vanilla DocTypes and are a
# strong smell when mentioned without the user asking. These are
# training-data prior giveaways.
_ERPNEXT_FIELD_SMELLS = (
	"customer_name",
	"taxes_and_charges",
	"sales_team",
	"grand_total",
	"transaction_date",
	"delivery_date",
	"order_type",
)

_DOCTYPE_NAME_RE = _re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,3})\b")

# Words that look like DocType names by capitalization but aren't
# doctypes (common English nouns, section headers). Exclude them from
# the foreign-doctype check.
_NON_DOCTYPE_CAPITALIZED = frozenset({
	"The", "This", "That", "These", "Those", "An", "A", "It", "Its",
	"Here", "There", "What", "When", "Where", "Why", "How",
	"Module", "Fields", "Field", "Permissions", "Permission", "Example",
	"Conclusion", "Usage", "Type", "Types", "Name", "Names", "Label",
	"Notes", "Note", "Description", "Required", "Yes", "No", "Draft",
	"Submit", "Cancel", "Save", "New", "Create", "Read", "Write",
	"Delete", "Approve", "Reject", "Active", "Inactive",
	"Python", "JavaScript", "JSON", "HTML", "SQL",
	"Frappe", "ERPNext", "API",
	"System", "Manager", "User", "Admin", "Administrator",
	"Final", "Answer", "Thought", "Action", "Observation",
	"I", "You", "We",
	"North", "South", "East", "West",
	"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
})


def _detect_drift(result_text: str, user_prompt: str) -> str | None:
	"""Return a short reason string if the output looks drifted, else None.

	Called by `_phase_post_crew` before extraction + rescue to catch
	training-data bleed and surface a specific error instead of the
	usual EMPTY_CHANGESET message.
	"""
	if not result_text or not isinstance(result_text, str):
		return None
	text = result_text.lower()
	prompt_lower = (user_prompt or "").lower()

	# Signal 1: ERPNext field smells the user never asked about
	for smell in _ERPNEXT_FIELD_SMELLS:
		if smell in text and smell not in prompt_lower:
			return f"output mentioned training-data field '{smell}' that the user never asked about"

	# Signal 2: "documentation mode" phrase
	doc_mode_hit = next(
		(p for p in _DOCUMENTATION_MODE_PHRASES if p in text),
		None,
	)

	# Signal 3: foreign DocType (Title-Cased multi-word token not in prompt)
	foreign_doctypes: list[str] = []
	if user_prompt:
		candidates = _DOCTYPE_NAME_RE.findall(result_text)
		for cand in candidates:
			# Filter out section headers, common English words, etc.
			first_word = cand.split()[0]
			if first_word in _NON_DOCTYPE_CAPITALIZED:
				continue
			if cand.lower() in prompt_lower:
				continue
			# Only count multi-word or clearly doctype-ish names. A single
			# capitalized word (e.g. "Draft") is too noisy.
			if " " in cand or len(cand) >= 6:
				foreign_doctypes.append(cand)

	# Combination rules
	if doc_mode_hit and foreign_doctypes:
		return (
			f"output slipped into documentation mode ('{doc_mode_hit}') about "
			f"{foreign_doctypes[0]!r} which is not in the user's request"
		)
	if doc_mode_hit and len(result_text) > 1500:
		return f"output is a long documentation dump containing '{doc_mode_hit}'"
	# A large prose output with no JSON at all is drift regardless
	if len(result_text) > 2000 and "{" not in result_text and "[" not in result_text:
		return "output is long prose with no JSON at all"
	# Too many foreign doctypes = the agent is clearly describing the
	# wrong thing even without a doc-mode giveaway
	if len(set(foreign_doctypes)) >= 3:
		return f"output references multiple unrelated doctypes: {sorted(set(foreign_doctypes))[:3]}"

	return None


@dataclass
class StopSignal:
	"""Instruction to abort the pipeline and surface a user-visible error."""

	error: str
	code: str
	extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineContext:
	"""All shared state threaded through the pipeline phases.

	Required at construction: the connection, the conversation id, and the
	raw user prompt. Everything else starts as a sensible default and is
	populated by the phases in execution order.
	"""

	conn: "ConnectionState"
	conversation_id: str
	prompt: str

	# Mode selection (three-mode chat feature).
	# manual_mode_override is the user's pick from the chat UI switcher
	# ("auto" | "dev" | "plan" | "insights"). The orchestrator phase
	# reads this, decides a final mode, and writes it to `mode` plus
	# `orchestrator_reason`. Chat/insights/plan modes skip the crew and
	# emit their own reply message types.
	manual_mode_override: str = "auto"
	mode: str = "dev"
	orchestrator_reason: str | None = None
	orchestrator_source: str | None = None
	chat_reply: str | None = None
	insights_reply: str | None = None
	plan_doc: dict | None = None

	# Services
	store: "StateStore | None" = None
	conversation_memory: "ConversationMemory | None" = None

	# Phase outputs (populated as the pipeline runs)
	user_context: dict = field(default_factory=dict)
	plan_pipeline_mode: str | None = None
	enhanced_prompt: str = ""
	clarify_qa_pairs: list[tuple[str, str]] = field(default_factory=list)
	pipeline_mode: str = "full"
	pipeline_mode_source: str = "site_config"
	custom_tools: dict | None = None
	crew: Any = None
	crew_state: "CrewState | None" = None
	crew_result: dict | None = None
	result_text: str = ""
	changes: list[dict] = field(default_factory=list)
	removed_by_reflection: list[dict] = field(default_factory=list)
	dry_run_result: dict = field(default_factory=dict)

	# Callbacks. `early_event_callback` is for pre-crew phases (sanitize,
	# enhance, clarify). `event_callback` is reused by the crew and all
	# post-crew phases. Both emit to the same WebSocket - the split is
	# historical and preserved to keep the UI event stream unchanged.
	early_event_callback: Callable | None = None
	event_callback: Callable | None = None

	# Control signals
	should_stop: bool = False
	stop_signal: StopSignal | None = None

	def stop(self, error: str, code: str, **extra: Any) -> None:
		self.should_stop = True
		self.stop_signal = StopSignal(error=error, code=code, extra=extra)


class AgentPipeline:
	"""Linear orchestrator over `PipelineContext`.

	Phase methods are resolved by name from `PHASES`. To add a phase: add a
	method `async def _my_phase(self):` and append its name to `PHASES`.
	"""

	PHASES: list[str] = [
		"sanitize",
		"load_state",
		"plan_check",
		"orchestrate",
		"enhance",
		"clarify",
		"resolve_mode",
		"build_crew",
		"run_crew",
		"post_crew",
	]

	def __init__(self, ctx: PipelineContext) -> None:
		self.ctx = ctx

	# ── Orchestrator ─────────────────────────────────────────────────

	async def run(self) -> None:
		"""Execute each phase in order. Catches timeouts + unexpected errors
		at the outer level and converts them to user-visible error messages."""
		try:
			for name in self.PHASES:
				if self.ctx.should_stop:
					break
				method = getattr(self, f"_phase_{name}")
				async with tracer.span(
					f"pipeline.{name}",
					conversation_id=self.ctx.conversation_id,
				):
					await method()
		except asyncio.TimeoutError:
			logger.error(
				"Pipeline timeout for conversation=%s", self.ctx.conversation_id
			)
			await self._send_error(
				"Pipeline timed out. The conversation has been saved - you can resume later.",
				"PIPELINE_TIMEOUT",
			)
			return
		except Exception as e:
			logger.error(
				"Pipeline error for conversation=%s: %s",
				self.ctx.conversation_id, e, exc_info=True,
			)
			await self._send_error(str(e), "PIPELINE_ERROR")
			return

		# If any phase signalled stop, emit the error now.
		if self.ctx.should_stop and self.ctx.stop_signal is not None:
			await self._send_error(
				self.ctx.stop_signal.error,
				self.ctx.stop_signal.code,
				**self.ctx.stop_signal.extra,
			)

	async def _send_error(self, error: str, code: str, **extra: Any) -> None:
		try:
			await self.ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {"error": error, "code": code, **extra},
			})
		except Exception as e:
			logger.warning("Failed to send error message: %s", e)

	# ── Phases ───────────────────────────────────────────────────────

	async def _phase_sanitize(self) -> None:
		from alfred.defense.sanitizer import check_prompt

		result = check_prompt(self.ctx.prompt)
		if not result["allowed"]:
			self.ctx.stop(
				error=result["rejection_reason"],
				code="PROMPT_BLOCKED" if not result["needs_review"] else "NEEDS_REVIEW",
			)

	async def _phase_load_state(self) -> None:
		"""Load redis store + per-conversation memory, capture user context."""
		from alfred.state.store import StateStore
		from alfred.state.conversation_memory import load_conversation_memory

		ctx = self.ctx
		redis = getattr(ctx.conn.websocket.app.state, "redis", None)
		ctx.store = StateStore(redis) if redis else None

		ctx.conversation_memory = await load_conversation_memory(
			ctx.store, ctx.conn.site_id, ctx.conversation_id
		)
		ctx.conversation_memory.add_prompt(ctx.prompt)

		ctx.user_context = {
			"user": ctx.conn.user,
			"roles": ctx.conn.roles,
			"site_id": ctx.conn.site_id,
		}

	async def _phase_plan_check(self) -> None:
		"""Ask the admin portal whether this site is allowed to run + what
		pipeline mode to use. Silent fallback if the portal isn't configured
		or returns an error."""
		ctx = self.ctx
		settings = ctx.conn.websocket.app.state.settings
		admin_url = getattr(settings, "ADMIN_PORTAL_URL", "")
		admin_key = getattr(settings, "ADMIN_SERVICE_KEY", "")
		if not admin_url or not admin_key:
			return

		try:
			from alfred.api.admin_client import AdminClient

			redis = getattr(ctx.conn.websocket.app.state, "redis", None)
			admin = AdminClient(admin_url, admin_key, redis)
			plan_result = await admin.check_plan(ctx.conn.site_id)

			if not plan_result.get("allowed", True):
				ctx.stop(
					error=plan_result.get("reason", "Plan limit exceeded"),
					code="PLAN_EXCEEDED",
					warning=plan_result.get("warning"),
				)
				return

			if plan_result.get("warning"):
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {
						"agent": "System", "status": "warning",
						"message": plan_result["warning"],
					},
				})

			raw_mode = (plan_result.get("pipeline_mode") or "").lower()
			if raw_mode in ("full", "lite"):
				ctx.plan_pipeline_mode = raw_mode
		except Exception as e:
			logger.warning("Plan check failed (allowing by default): %s", e)

	async def _phase_orchestrate(self) -> None:
		"""Classify the prompt into a mode (dev/plan/insights/chat).

		Gated by ALFRED_ORCHESTRATOR_ENABLED. When the flag is unset the
		pipeline behaves exactly as before (mode stays "dev", no LLM call,
		no skip). Chat mode short-circuits: the handler runs inline, emits
		a chat_reply message, and the pipeline stops via ctx.should_stop.
		"""
		import os as _os

		ctx = self.ctx

		if _os.environ.get("ALFRED_ORCHESTRATOR_ENABLED") != "1":
			# Feature flag off - preserve pre-feature behavior.
			ctx.mode = "dev"
			return

		from alfred.orchestrator import classify_mode

		decision = await classify_mode(
			prompt=ctx.prompt,
			memory=ctx.conversation_memory,
			manual_override=ctx.manual_mode_override,
			site_config=ctx.conn.site_config or {},
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
		except Exception as e:
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
		from alfred.state.conversation_memory import save_conversation_memory

		ctx = self.ctx

		try:
			reply = await handle_chat(
				prompt=ctx.prompt,
				memory=ctx.conversation_memory,
				user_context=ctx.user_context,
				site_config=ctx.conn.site_config or {},
			)
		except Exception as e:
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
		except Exception as e:
			logger.warning("chat_reply send failed: %s", e)

		# Persist conversation memory so follow-up turns see this exchange.
		if ctx.conversation_memory is not None and ctx.store is not None:
			try:
				await save_conversation_memory(
					ctx.store,
					ctx.conn.site_id,
					ctx.conversation_id,
					ctx.conversation_memory,
				)
			except Exception as e:
				logger.warning("chat memory save failed: %s", e)

	async def _run_insights_short_circuit(self) -> None:
		"""Run the insights handler inline and emit an insights_reply message.

		Insights mode spins up a single-agent crew with read-only MCP tools,
		runs it with a tight tool budget (5 calls), and emits the final
		markdown reply. Never produces a changeset, never writes to the DB.
		"""
		from alfred.handlers.insights import handle_insights
		from alfred.state.conversation_memory import save_conversation_memory

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
			except Exception as e:
				logger.warning("insights event_callback send failed: %s", e)

		ctx.event_callback = _event_cb

		try:
			reply = await handle_insights(
				prompt=ctx.prompt,
				conn=ctx.conn,
				conversation_id=ctx.conversation_id,
				user_context=ctx.user_context,
				event_callback=_event_cb,
			)
		except Exception as e:
			logger.warning("Insights handler raised: %s", e, exc_info=True)
			reply = (
				"I had trouble looking that up on your site just now. "
				"Try again in a moment, or rephrase your question."
			)

		ctx.insights_reply = reply

		try:
			await ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "insights_reply",
				"data": {
					"conversation": ctx.conversation_id,
					"reply": reply,
					"mode": "insights",
				},
			})
		except Exception as e:
			logger.warning("insights_reply send failed: %s", e)

		# Record the Q/A pair in conversation memory so later Plan/Dev turns
		# can resolve references like "that workflow I asked about".
		if ctx.conversation_memory is not None:
			try:
				ctx.conversation_memory.add_insights_query(ctx.prompt, reply)
			except Exception as e:
				logger.warning("insights memory record failed: %s", e)

		if ctx.conversation_memory is not None and ctx.store is not None:
			try:
				await save_conversation_memory(
					ctx.store,
					ctx.conn.site_id,
					ctx.conversation_id,
					ctx.conversation_memory,
				)
			except Exception as e:
				logger.warning("insights memory save failed: %s", e)

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
		except Exception as e:
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
		except Exception as e:
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
		from alfred.state.conversation_memory import save_conversation_memory

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
			except Exception as e:
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
		except Exception as e:
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
		except Exception as e:
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
			except Exception as e:
				logger.warning("plan memory record failed: %s", e)

		if ctx.conversation_memory is not None and ctx.store is not None:
			try:
				await save_conversation_memory(
					ctx.store,
					ctx.conn.site_id,
					ctx.conversation_id,
					ctx.conversation_memory,
				)
			except Exception as e:
				logger.warning("plan memory save failed: %s", e)

	async def _phase_enhance(self) -> None:
		if self.ctx.mode != "dev":
			# Enhance is a Dev-mode concern. Plan/Insights/Chat have their
			# own entry paths.
			return

		from alfred.agents.prompt_enhancer import enhance_prompt

		ctx = self.ctx
		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_status",
			"data": {
				"agent": "System", "status": "enhancing",
				"message": "Analyzing your request...",
			},
		})

		# Phase C: Plan -> Dev handoff. If the conversation has an active
		# plan doc (from a prior Plan-mode run) and the current prompt
		# looks like an approval, flip the plan's status to "approved"
		# BEFORE rendering the memory context block so `render_for_prompt`
		# emits the full step list (only approved plans render their
		# steps verbatim - see conversation_memory.render_for_prompt).
		self._maybe_approve_active_plan()

		conversation_context = (
			ctx.conversation_memory.render_for_prompt()
			if ctx.conversation_memory else ""
		)

		ctx.enhanced_prompt = await enhance_prompt(
			ctx.prompt, ctx.user_context, ctx.conn.site_config,
			conversation_context=conversation_context or None,
		)

		# Bind the early event callback so the clarifier (next phase) can
		# stream updates before the full event_callback is set up.
		async def _early_cb(event_type: str, data: dict) -> None:
			try:
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {"event": event_type, **data},
				})
			except Exception as e:
				logger.warning("early event_callback send failed: %s", e)

		ctx.early_event_callback = _early_cb

	async def _phase_clarify(self) -> None:
		if self.ctx.mode != "dev":
			return

		from alfred.api.websocket import _clarify_requirements

		ctx = self.ctx
		ctx.enhanced_prompt, ctx.clarify_qa_pairs = await _clarify_requirements(
			ctx.enhanced_prompt, ctx.conn, ctx.early_event_callback
		)
		if ctx.clarify_qa_pairs and ctx.conversation_memory is not None:
			ctx.conversation_memory.add_clarifications(ctx.clarify_qa_pairs)

	async def _phase_resolve_mode(self) -> None:
		"""Pick full vs lite. Admin-portal override beats site config which
		beats the 'full' default. Cost tier, NOT the dev/plan/insights/chat mode."""
		if self.ctx.mode != "dev":
			return

		ctx = self.ctx
		if ctx.plan_pipeline_mode:
			ctx.pipeline_mode = ctx.plan_pipeline_mode
			ctx.pipeline_mode_source = "plan"
		else:
			mode = (ctx.conn.site_config.get("pipeline_mode") or "full").lower()
			if mode not in ("full", "lite"):
				mode = "full"
			ctx.pipeline_mode = mode
			ctx.pipeline_mode_source = "site_config"

		logger.info(
			"Pipeline mode resolved for %s: %s (source=%s)",
			ctx.conn.site_id, ctx.pipeline_mode, ctx.pipeline_mode_source,
		)

		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_status",
			"data": {
				"agent": "Orchestrator", "status": "started",
				"phase": "requirement",
				"pipeline_mode": ctx.pipeline_mode,
				"pipeline_mode_source": ctx.pipeline_mode_source,
			},
		})

	async def _phase_build_crew(self) -> None:
		"""Assemble the crew, MCP tool set, and per-run tracking state."""
		if self.ctx.mode != "dev":
			return

		import os as _os

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
			except Exception as e:
				logger.debug("Failed to clear prior crew state (ignored): %s", e)

		ctx.custom_tools = (
			build_mcp_tools(ctx.conn.mcp_client) if ctx.conn.mcp_client else None
		)

		# Phase 1 per-run MCP tracking state (budget, dedup, failure counter).
		if (
			ctx.conn.mcp_client is not None
			and _os.environ.get("ALFRED_PHASE1_DISABLED") != "1"
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
		timeout = ctx.conn.site_config.get("task_timeout_seconds", 300)
		ctx.crew_result = await asyncio.wait_for(
			run_crew(
				ctx.crew, ctx.crew_state, ctx.store,
				ctx.conn.site_id, ctx.conversation_id, ctx.event_callback,
			),
			timeout=timeout * 2,
		)

	async def _phase_post_crew(self) -> None:
		"""Extract changeset, rescue if needed, reflect, dry-run, send preview."""
		if self.ctx.mode != "dev":
			return

		from alfred.api.websocket import (
			_extract_changes,
			_rescue_regenerate_changeset,
			_dry_run_with_retry,
		)
		from alfred.agents.reflection import reflect_minimality
		from alfred.state.conversation_memory import save_conversation_memory

		ctx = self.ctx
		result = ctx.crew_result or {}

		if result.get("status") != "completed":
			self._send_error_later(
				result.get("error", "Pipeline failed"),
				"PIPELINE_FAILED",
			)
			return

		ctx.result_text = result.get("result", "") or ""

		# Emit a tight "crew completed" status. Do NOT leak result_text
		# here - if the crew drifted into prose (e.g. training-data
		# Sales Order documentation), sending result_text[:2000] would
		# show the drift to the user even when the rescue path later
		# succeeds. The preview panel is the canonical place for the
		# final changeset; result_text stays server-side for logs.
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

		# Drift detection: qwen2.5-coder:32b sometimes slips out of the
		# task structure and regurgitates training-data Frappe docs
		# (typically a full Sales Order schema dump). If the Developer's
		# output is clearly off-topic, fail loudly instead of streaming
		# the garbage to the UI or feeding it into rescue which itself
		# would just drift in sympathy.
		drift_reason = _detect_drift(ctx.result_text, ctx.prompt)

		# Extract
		ctx.changes = _extract_changes(ctx.result_text) if not drift_reason else []

		# Rescue path: if the crew drifted into prose, regenerate from the
		# original prompt in one focused LLM call.
		if not ctx.changes:
			logger.info(
				"First-pass extraction empty (drift=%s) - attempting rescue "
				"regeneration from original prompt",
				drift_reason or "no",
			)
			ctx.changes = await _rescue_regenerate_changeset(
				ctx.enhanced_prompt, ctx.result_text,
				ctx.conn.site_config, ctx.event_callback,
				user_prompt=ctx.prompt,
				drift_reason=drift_reason,
			)

		if not ctx.changes:
			logger.warning(
				"Pipeline completed but extraction + rescue both returned "
				"empty. Drift=%s. Result text (first 500): %r",
				drift_reason or "no",
				ctx.result_text[:500],
			)
			user_message = (
				"Alfred couldn't produce a valid changeset for your request. "
				"This usually means the agent drifted off-topic. Try rephrasing "
				"with the exact DocType name and the exact rule, e.g. "
				"\"On Employee DocType, before insert, throw an error if age "
				"is less than 24.\""
			)
			if drift_reason:
				user_message = (
					f"Alfred's output was off-topic ({drift_reason}). "
					"The rescue path also couldn't produce a valid changeset. "
					"Please rephrase with the exact DocType name and the exact "
					"rule, e.g. \"On Employee DocType, before insert, throw an "
					"error if age is less than 24.\""
				)
			self._send_error_later(
				user_message,
				"EMPTY_CHANGESET",
				drift_reason=drift_reason or "",
			)
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
			self._mark_active_plan_built_if_any()
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
			},
		})

	def _send_error_later(self, error: str, code: str, **extra: Any) -> None:
		"""Mark the context as stopped so `run()` emits the error on exit.

		Used from `_phase_post_crew` when the crew completed but produced
		no usable changeset - we want the same error-send shape the outer
		try/except gives us, so we route through the stop signal.
		"""
		self.ctx.stop(error=error, code=code, **extra)
