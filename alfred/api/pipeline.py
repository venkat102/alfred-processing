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
import os as _os_for_flag
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

# Cap on how many target DocTypes we'll deep-recon per turn. Users rarely
# ask about more than one in a single prompt; capping prevents a prompt
# that name-drops 6 DocTypes from triggering 6 MCP calls and blowing the
# inject-banner budget.
_INJECT_MAX_TARGETS = 2
# Max chars of site-state banner content per turn (not counting the
# decorative header/footer). At ~4 chars/token this is roughly 500 tokens.
_INJECT_SITE_BUDGET = 2000

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


def _extract_target_doctypes(prompt: str, limit: int = _INJECT_MAX_TARGETS) -> list[str]:
	"""Pull likely-target DocType names out of an enhanced prompt.

	Uses the same regex + noise-word filter as `_detect_drift` so extraction
	is consistent across the two call sites. A candidate is kept when:
	  - it isn't in _NON_DOCTYPE_CAPITALIZED (common English, section headers)
	  - it has a space (multi-word) OR is >= 6 chars (single-word DocTypes
	    like "Employee", "Customer", "ToDo" - shorter tokens are noise)
	  - it hasn't already been picked (dedup, first-occurrence-wins)

	Does NOT validate against the framework KG here - that's left to the
	site-detail MCP call, which already returns {error: not_found} for
	unknown DocTypes. Avoiding the extra MCP round-trip is important for
	pipeline latency (this runs on every Dev-mode turn).

	Returns up to `limit` candidates, preserving the order they appear in
	the prompt.
	"""
	if not prompt:
		return []
	picked: list[str] = []
	seen: set[str] = set()
	for cand in _DOCTYPE_NAME_RE.findall(prompt):
		if cand in seen:
			continue
		first_word = cand.split()[0]
		if first_word in _NON_DOCTYPE_CAPITALIZED:
			continue
		# Single-word candidate must be long enough to be a real DocType
		# name. "Draft", "Python" etc. are already in the exclude list; this
		# catches residual short capitalised words like "API" or "HR".
		if " " not in cand and len(cand) < 6:
			continue
		picked.append(cand)
		seen.add(cand)
		if len(picked) >= limit:
			break
	return picked


def _site_detail_has_artefacts(detail: dict) -> bool:
	"""True if a site-detail dict contains at least one artefact worth rendering.

	Prevents rendering a "SITE STATE FOR X" block for a DocType that exists
	but has zero customizations on this site - would just be banner noise.
	"""
	if not isinstance(detail, dict):
		return False
	for key in ("workflows", "server_scripts", "custom_fields", "notifications", "client_scripts"):
		if detail.get(key):
			return True
	return False


def _render_site_state_block(doctype: str, detail: dict, budget: int) -> str:
	"""Format one DocType's site-state into a compact banner block.

	Relevance order (highest-signal first so low-value artefacts get truncated
	when the budget runs low):
	  1. Workflows (graph is the most structural artefact)
	  2. Server Scripts (logic that might collide with user's request)
	  3. Custom Fields (schema extension)
	  4. Notifications (communication - lower priority for Dev mode)
	  5. Client Scripts (UI, lowest priority)

	`budget` is the max chars this function may emit (decorative header/footer
	excluded). As we render, we track cumulative length and stop adding new
	artefacts once the next one would push past the budget - callers get a
	"... (N more)" footer line so the agent knows there's unseen state.
	"""
	lines: list[str] = []
	remaining = budget
	truncated_kinds: list[tuple[str, int]] = []

	def _try_add(block: str) -> bool:
		nonlocal remaining
		if len(block) + 1 > remaining:
			return False
		lines.append(block)
		remaining -= len(block) + 1
		return True

	# Workflows - full graph
	for wf in detail.get("workflows") or []:
		states = wf.get("states") or []
		transitions = wf.get("transitions") or []
		state_line = " -> ".join(
			f"{s.get('state')} [{s.get('allow_edit') or '-'}]"
			for s in states
		) or "-"
		txn_summary = (
			", ".join(
				f"{t.get('state')} --{t.get('action')}--> {t.get('next_state')}"
				for t in transitions[:4]
			)
			+ (f" (+{len(transitions) - 4} more)" if len(transitions) > 4 else "")
		) if transitions else "no transitions"
		active = "active" if wf.get("is_active") else "inactive"
		block = (
			f"Workflow: {wf.get('name')} ({active}, field: "
			f"{wf.get('workflow_state_field') or '-'})\n"
			f"  states: {state_line}\n"
			f"  transitions: {txn_summary}"
		)
		if not _try_add(block):
			truncated_kinds.append(("workflow", 1))
			break

	# Server Scripts - body preview
	scripts = detail.get("server_scripts") or []
	# Active first, then disabled
	scripts = sorted(scripts, key=lambda s: int(s.get("disabled") or 0))
	for idx, s in enumerate(scripts):
		state = "disabled" if s.get("disabled") else "enabled"
		# Indent the body snippet so it reads as a nested block
		body = (s.get("script") or "").strip()
		body_indented = "\n".join("    " + ln for ln in body.splitlines()[:8]) or "    (empty)"
		block = (
			f"Server Script: {s.get('name')} "
			f"({s.get('doctype_event') or s.get('script_type') or '?'}, {state})\n"
			f"  body preview:\n{body_indented}"
		)
		if not _try_add(block):
			truncated_kinds.append(("server_script", len(scripts) - idx))
			break

	# Custom Fields - one line each
	fields = detail.get("custom_fields") or []
	if fields:
		field_lines: list[str] = []
		for f in fields:
			opt = f.get("options")
			ft = f.get("fieldtype")
			extra = f" (options: {opt.replace(chr(10), ',')})" if opt and ft in ("Select", "Link") else ""
			reqd = ", required" if f.get("reqd") else ""
			field_lines.append(
				f"  - {f.get('fieldname')} ({ft}{reqd}) label={f.get('label')!r}{extra}"
			)
		block = "Custom Fields:\n" + "\n".join(field_lines)
		if not _try_add(block):
			truncated_kinds.append(("custom_field", len(fields)))

	# Notifications
	notifs = detail.get("notifications") or []
	if notifs:
		notif_lines = [
			f"  - {n.get('name')} ({n.get('event')}, {n.get('channel')}): "
			f"{n.get('subject')!r}"
			for n in notifs
		]
		block = "Notifications:\n" + "\n".join(notif_lines)
		if not _try_add(block):
			truncated_kinds.append(("notification", len(notifs)))

	# Client Scripts - headline only
	clients = detail.get("client_scripts") or []
	if clients:
		client_lines = [
			f"  - {c.get('name')} (view: {c.get('view')}, "
			f"{'enabled' if c.get('enabled') else 'disabled'})"
			for c in clients
		]
		block = "Client Scripts:\n" + "\n".join(client_lines)
		if not _try_add(block):
			truncated_kinds.append(("client_script", len(clients)))

	if truncated_kinds:
		tail = ", ".join(f"{kind}: {n}" for kind, n in truncated_kinds)
		lines.append(f"(more artefacts omitted for brevity: {tail})")

	body = "\n\n".join(lines) if lines else "(no major artefacts)"
	return (
		f'=== SITE STATE FOR "{doctype}" (already on this site) ===\n'
		f"DO NOT propose anything that conflicts with or duplicates these "
		f"existing customizations. Extend, replace, or build atop them as "
		f"appropriate.\n\n"
		f"{body}\n"
		f"=========================================================="
	)


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

	# Per-intent Builder classification (dev mode only).
	# Written by _phase_classify_intent, consumed by _phase_build_crew to
	# select a specialist Developer agent, and by _phase_post_crew to
	# backfill registry defaults into ctx.changes. "unknown" or None means
	# no specialist routing; behaviour matches flag-off. See
	# docs/specs/2026-04-21-doctype-builder-specialist.md.
	intent: str | None = None
	intent_source: str | None = None
	intent_confidence: str | None = None
	intent_reason: str | None = None

	# Per-module Builder classification (dev mode only, V2).
	# Written by _phase_classify_module when both V1 (ALFRED_PER_INTENT_BUILDERS)
	# and V2 (ALFRED_MODULE_SPECIALISTS) flags are on. Flows into
	# _phase_provide_module_context, _phase_build_crew, and _phase_post_crew.
	# Spec: docs/specs/2026-04-22-module-specialists.md.
	module: str | None = None
	module_confidence: str | None = None
	module_source: str | None = None
	module_reason: str | None = None
	module_target_doctype: str | None = None
	module_context: str = ""
	module_validation_notes: list[dict] = field(default_factory=list)

	# Services
	store: "StateStore | None" = None
	conversation_memory: "ConversationMemory | None" = None

	# Phase outputs (populated as the pipeline runs)
	user_context: dict = field(default_factory=dict)
	plan_pipeline_mode: str | None = None
	enhanced_prompt: str = ""
	clarify_qa_pairs: list[tuple[str, str]] = field(default_factory=list)
	# Frappe KB auto-inject (Phase B). `injected_kb` is the list of entry ids
	# (e.g. ["server_script_no_imports"]) that were matched by keyword scan on
	# `enhanced_prompt` and prepended to it as a reference banner before the
	# crew runs. Empty list = no match / keyword score below threshold / KB
	# tool not available. Logged by the tracer so a still-wrong output can be
	# triaged as "rule wasn't injected" vs. "rule was injected but ignored".
	injected_kb: list[str] = field(default_factory=list)
	# Site reconnaissance auto-inject (Phase B.5). Per-DocType deep recon
	# fetched via get_site_customization_detail for each DocType mentioned
	# in `enhanced_prompt`. Empty = no target DocType extracted / MCP call
	# failed / DocType has no customizations. Keyed by DocType name.
	injected_site_state: dict = field(default_factory=dict)
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


def _summarise_probe_error(exc: Exception) -> str:
	"""Render a one-line reason for a warmup probe failure.

	Keeps the message short enough to fit a chat toast without leaking the
	full stack trace. HTTP status + body excerpt for HTTPError, bare str()
	for everything else.
	"""
	import urllib.error as _urllib_error

	if isinstance(exc, _urllib_error.HTTPError):
		try:
			body = (exc.read() or b"").decode(errors="replace")[:200]
		except Exception:
			body = ""
		return f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}"
	return str(exc) or exc.__class__.__name__


class AgentPipeline:
	"""Linear orchestrator over `PipelineContext`.

	Phase methods are resolved by name from `PHASES`. To add a phase: add a
	method `async def _my_phase(self):` and append its name to `PHASES`.
	"""

	PHASES: list[str] = [
		"sanitize",
		"load_state",
		"warmup",
		"plan_check",
		"orchestrate",
		"classify_intent",
		"classify_module",
		"enhance",
		"clarify",
		"inject_kb",
		"resolve_mode",
		"provide_module_context",
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
		import time as _time

		from alfred.obs.metrics import pipeline_phase_duration_seconds

		try:
			for name in self.PHASES:
				if self.ctx.should_stop:
					break
				method = getattr(self, f"_phase_{name}")
				phase_started = _time.perf_counter()
				async with tracer.span(
					f"pipeline.{name}",
					conversation_id=self.ctx.conversation_id,
				):
					try:
						await method()
					finally:
						pipeline_phase_duration_seconds.labels(phase=name).observe(
							_time.perf_counter() - phase_started
						)
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
		# A user-initiated cancel is not an error. Surface it as a distinct
		# WS event so the UI can render it as "Run cancelled" rather than the
		# generic error banner, and so the rescue/retry path stays dormant.
		if code == "user_cancel":
			try:
				await self.ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "run_cancelled",
					"data": {"reason": error, **extra},
				})
			except Exception as e:
				logger.warning("Failed to send cancellation message: %s", e)
			return
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

	async def _phase_warmup(self) -> None:
		"""Pre-warm + health-probe Ollama models that this pipeline will use.

		Fires a minimal /api/generate call (1 token) for each distinct Ollama
		model the pipeline needs. Ollama loads the model on first request and
		keeps it warm for 5 minutes; we pass keep_alive=10m to extend that past
		the full pipeline duration.

		Doubles as a strict health gate: if any probe fails (connection refused,
		timeout, 500 from a dead model runner), we stop the pipeline here with
		OLLAMA_UNHEALTHY rather than let the crew burn 2-3 minutes of retries
		per agent. Cloud providers (no `ollama/` prefix) are skipped - this
		check only applies to self-hosted Ollama.
		"""
		import urllib.request as _urllib_request

		ctx = self.ctx
		site_config = ctx.conn.site_config or {}

		from alfred.llm_client import (
			TIER_AGENT, TIER_REASONING, TIER_TRIAGE,
			_resolve_ollama_config_for_tier,
		)

		models_to_probe: set[tuple[str, str]] = set()
		for tier in (TIER_TRIAGE, TIER_REASONING, TIER_AGENT):
			model, base_url, _ = _resolve_ollama_config_for_tier(site_config, tier)
			if model.startswith("ollama/"):
				models_to_probe.add((model.removeprefix("ollama/"), base_url))

		if not models_to_probe:
			# Cloud-only configuration - nothing to probe.
			return

		async def _probe_one(ollama_model: str, base_url: str):
			payload = json.dumps({
				"model": ollama_model,
				"prompt": "hi",
				"stream": False,
				"keep_alive": "10m",
				"options": {"num_predict": 1},
			}).encode()
			url = f"{base_url.rstrip('/')}/api/generate"
			req = _urllib_request.Request(
				url, data=payload,
				headers={"Content-Type": "application/json"},
			)
			loop = asyncio.get_running_loop()
			await loop.run_in_executor(
				None, lambda: _urllib_request.urlopen(req, timeout=30)
			)

		tasks = [
			asyncio.create_task(_probe_one(m, u))
			for m, u in models_to_probe
		]
		results = await asyncio.gather(*tasks, return_exceptions=True)

		warmed: list[str] = []
		failures: list[tuple[str, str, str]] = []  # (model, base_url, reason)
		for (model, base_url), result in zip(models_to_probe, results):
			if isinstance(result, Exception):
				reason = _summarise_probe_error(result)
				logger.warning(
					"Ollama health probe failed for %s at %s: %s",
					model, base_url, reason,
				)
				failures.append((model, base_url, reason))
			else:
				warmed.append(model)

		if failures:
			from alfred.obs.metrics import llm_errors_total

			# Record each distinct failure as an llm_errors_total dimension so
			# the rate of OLLAMA_UNHEALTHY is visible on the /metrics scrape.
			for _, _, reason in failures:
				llm_errors_total.labels(
					tier="warmup", error_type="OLLAMA_UNHEALTHY",
				).inc()
			first_model, first_url, first_reason = failures[0]
			count = len(failures)
			plural = "s" if count > 1 else ""
			ctx.stop(
				error=(
					f"Processing service is unavailable: {count} Ollama "
					f"model{plural} failed health check. First failure: "
					f"{first_model} at {first_url} ({first_reason}). "
					"Contact your admin."
				),
				code="OLLAMA_UNHEALTHY",
				failed_models=[
					{"model": m, "base_url": u, "reason": r}
					for m, u, r in failures
				],
			)
			return

		if warmed:
			logger.info("Pre-warmed %d model(s): %s", len(warmed), ", ".join(warmed))

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

	async def _phase_classify_intent(self) -> None:
		"""Classify the dev-mode prompt into a Builder specialist intent.

		No-op for non-dev modes and when ALFRED_PER_INTENT_BUILDERS is unset
		(``_phase_build_crew`` and the backfill in ``_phase_post_crew`` are
		also flag-gated, so an off flag means zero behavioural change from
		pre-feature Alfred). Stores the IntentDecision fields on ctx.intent*
		for downstream phases to read.

		See docs/specs/2026-04-21-doctype-builder-specialist.md.
		"""
		import os as _os

		ctx = self.ctx
		if ctx.mode != "dev":
			return
		if _os.environ.get("ALFRED_PER_INTENT_BUILDERS") != "1":
			return

		from alfred.orchestrator import classify_intent

		decision = await classify_intent(
			prompt=ctx.prompt,
			site_config=ctx.conn.site_config or {},
		)
		ctx.intent = decision.intent
		ctx.intent_source = decision.source
		ctx.intent_confidence = decision.confidence
		ctx.intent_reason = decision.reason

		logger.info(
			"Intent decision for conversation=%s: intent=%s source=%s confidence=%s reason=%r",
			ctx.conversation_id, decision.intent, decision.source,
			decision.confidence, decision.reason,
		)

	async def _phase_classify_module(self) -> None:
		"""Classify the dev-mode prompt's target module for specialist selection.

		No-op for non-dev modes, when ALFRED_PER_INTENT_BUILDERS is off, or
		when ALFRED_MODULE_SPECIALISTS is off. Stores the ModuleDecision
		fields on ctx.module* for downstream phases to read.

		See docs/specs/2026-04-22-module-specialists.md.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		if _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") != "1":
			return
		if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") != "1":
			return

		from alfred.orchestrator import detect_module

		# Heuristic: use the first extracted target DocType so module
		# detection can take the high-confidence path (target_doctype
		# match) rather than falling back to keyword hints.
		targets = _extract_target_doctypes(ctx.prompt)
		first_target = targets[0] if targets else None

		decision = await detect_module(
			prompt=ctx.prompt,
			target_doctype=first_target,
			site_config=ctx.conn.site_config or {},
		)
		ctx.module = decision.module
		ctx.module_source = decision.source
		ctx.module_confidence = decision.confidence
		ctx.module_reason = decision.reason
		ctx.module_target_doctype = first_target

		logger.info(
			"Module decision for conversation=%s: module=%s source=%s confidence=%s reason=%r",
			ctx.conversation_id, decision.module, decision.source,
			decision.confidence, decision.reason,
		)

	async def _phase_provide_module_context(self) -> None:
		"""Invoke module specialist's context pass; stash snippet on ctx.

		No-op when flags off or when no module was detected.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		if _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") != "1":
			return
		if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") != "1":
			return
		if not ctx.module:
			return

		from alfred.agents.specialists.module_specialist import provide_context

		# Surface the Redis client (if configured) to the specialist so
		# the 5-min context cache is shared across workers. When Redis is
		# unreachable or not configured, the specialist falls back to a
		# process-local cache automatically.
		redis = getattr(getattr(ctx.conn, "websocket", None), "app", None)
		redis = getattr(getattr(redis, "state", None), "redis", None)

		try:
			snippet = await provide_context(
				module=ctx.module,
				intent=ctx.intent or "unknown",
				target_doctype=ctx.module_target_doctype,
				site_config=ctx.conn.site_config or {},
				redis=redis,
			)
		except Exception as e:
			logger.warning(
				"provide_module_context failed for conversation=%s module=%s: %s",
				ctx.conversation_id, ctx.module, e,
			)
			snippet = ""

		ctx.module_context = snippet

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

	async def _phase_inject_kb(self) -> None:
		"""Auto-inject Frappe KB entries + site state into the enhanced prompt.

		Two retrievals, one combined banner:
		  (1) FKB keyword search (Phase B) - platform rules, APIs, idioms
		      matching the user's enhanced prompt.
		  (2) Site reconnaissance (Phase B.5) - for each DocType named in the
		      prompt, fetch existing artefacts (workflows, server scripts,
		      custom fields, notifications, client scripts) via the
		      get_site_customization_detail MCP tool.

		Both retrievals run off the same source-of-truth (the user's enhanced
		prompt, BEFORE we prepend anything). Their renderings are concatenated
		into a single banner:

		    === FRAPPE KB CONTEXT ===
		    [rule/api/idiom entries]
		    ==========================

		    === SITE STATE FOR "Employee" ===
		    [existing artefacts on the target DocType]
		    ==================================

		    --- USER REQUEST ---
		    [original enhanced prompt]

		Skips when mode != "dev" / stop signal set / no MCP client / empty
		prompt. Individual retrievals fail open - if FKB search errors out,
		site-state still injects; if site-detail fails for one target, the
		other target still gets recon. A total failure just leaves the
		enhanced_prompt unchanged and lets the crew run with the pre-Phase-E
		hardcoded rule blocks.
		"""
		ctx = self.ctx
		span = tracer.current()

		if ctx.mode != "dev":
			if span: span.set(skipped="not-dev-mode")
			return
		if ctx.should_stop:
			if span: span.set(skipped="stop-signal")
			return
		if not ctx.enhanced_prompt:
			if span: span.set(skipped="empty-prompt")
			logger.debug("inject_kb: empty enhanced_prompt - skipping")
			return
		# Note: no MCP-client short-circuit. FKB retrieval is local; only
		# site-recon needs MCP. The site-recon block below guards itself.

		# Snapshot the source prompt BEFORE we prepend anything. Target
		# DocType extraction and FKB search both run off this snapshot so
		# neither sees doctype-looking names we add in the banners.
		source_prompt = ctx.enhanced_prompt

		# ── (1) FKB hybrid search (local, no MCP round-trip) ──────────
		# Phase C: retrieval moved from the Frappe-side MCP tool to the
		# processing-local `alfred.knowledge.fkb` module so we can layer
		# semantic (sentence-transformers) on top of keyword without
		# installing ML deps in the bench venv. The module reads the same
		# YAML source-of-truth and falls back to keyword-only if the model
		# can't load (e.g. missing weights, import error) - the pipeline
		# keeps running either way.
		from alfred.knowledge import fkb as _fkb

		fkb_rendered: list[str] = []
		try:
			fkb_hits = _fkb.search_hybrid(source_prompt, k=3)
		except Exception as e:
			if span: span.set(fkb_error=f"fkb:{type(e).__name__}")
			logger.warning("inject_kb: FKB search failed: %s", e)
			fkb_hits = []

		for entry in fkb_hits:
			if not isinstance(entry, dict):
				continue
			entry_id = entry.get("id") or "<unknown>"
			kind = entry.get("kind") or "entry"
			title = (entry.get("title") or "").strip()
			summary = (entry.get("summary") or "").strip()
			body = (entry.get("body") or "").strip()
			# `_mode` is "keyword" or "semantic" - include it in the block
			# header so the agent (and traces) can see which retriever
			# surfaced each entry.
			mode_tag = entry.get("_mode", "kb")

			block_parts = [f"[{kind}: {entry_id} via {mode_tag}] {title}"]
			if summary:
				block_parts.append(f"Summary: {summary}")
			if body:
				block_parts.append(body)
			fkb_rendered.append("\n".join(block_parts))
			ctx.injected_kb.append(entry_id)

		# ── (2) Site reconnaissance (requires MCP) ────────────────────
		# Unlike FKB (local YAML), site-recon queries the live Frappe app
		# via MCP. Skip cleanly if no MCP client is wired - FKB still
		# injects.
		site_rendered: list[str] = []
		site_artefact_count = 0
		if ctx.conn.mcp_client:
			targets = _extract_target_doctypes(source_prompt)
			# Per-DocType char budget - split the total budget evenly so one
			# heavily-customized DocType can't starve the other.
			per_target_budget = (
				_INJECT_SITE_BUDGET // max(len(targets), 1) if targets else 0
			)
			for doctype in targets:
				try:
					detail = await ctx.conn.mcp_client.call_tool(
						"get_site_customization_detail",
						{"doctype": doctype},
					)
				except Exception as e:
					logger.warning(
						"inject_kb: site-detail call failed for %r: %s", doctype, e,
					)
					continue
				if not isinstance(detail, dict) or detail.get("error"):
					# Unknown DocType, permission denied, or infra error - silently
					# skip. Don't treat "not_found" as a real failure; it's the
					# common case for targets extracted from prompt noise.
					continue
				if not _site_detail_has_artefacts(detail):
					# DocType exists but has no customizations on this site - no
					# point injecting a block that says "nothing to see here".
					continue

				ctx.injected_site_state[doctype] = detail
				site_rendered.append(
					_render_site_state_block(doctype, detail, per_target_budget)
				)
				site_artefact_count += sum(
					len(detail.get(k) or [])
					for k in ("workflows", "server_scripts", "custom_fields",
							  "notifications", "client_scripts")
				)

		# ── (3) Combine + prepend ─────────────────────────────────────
		parts: list[str] = []

		if fkb_rendered:
			parts.append(
				"=== FRAPPE KB CONTEXT (auto-injected, reference only) ===\n"
				"The following platform rules / APIs / idioms were retrieved from\n"
				"the Frappe Knowledge Base based on your request. Follow them\n"
				"alongside the user request; they are NOT part of the request.\n\n"
				+ "\n---\n".join(fkb_rendered)
				+ "\n=========================================================="
			)

		if site_rendered:
			parts.extend(site_rendered)

		if not parts:
			# Nothing to inject from either layer. Record the no-op state so
			# the tracer can distinguish "injected nothing" from "phase didn't
			# run".
			if span:
				span.set(
					injected_kb=[],
					injected_count=0,
					injected_site_doctypes=[],
					injected_site_count=0,
				)
			return

		banner = (
			"\n\n".join(parts)
			+ "\n\n--- USER REQUEST (interpret this verbatim) ---\n"
		)
		ctx.enhanced_prompt = banner + source_prompt

		if span:
			span.set(
				injected_kb=list(ctx.injected_kb),
				injected_count=len(ctx.injected_kb),
				injected_site_doctypes=list(ctx.injected_site_state.keys()),
				injected_site_count=site_artefact_count,
				banner_chars=len(banner),
			)
		logger.info(
			"inject_kb: FKB=%s site=%s for conversation=%s",
			ctx.injected_kb,
			list(ctx.injected_site_state.keys()),
			ctx.conversation_id,
		)

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
		if drift_reason:
			# Counter: quantify how often the framework-quirk tax fires.
			try:
				from alfred.obs.metrics import crew_drift_total
				crew_drift_total.labels(reason=drift_reason).inc()
			except Exception:
				pass

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
			# Counter: did rescue actually recover a changeset, or are we
			# just burning tokens on a lost cause?
			try:
				from alfred.obs.metrics import crew_rescue_total
				crew_rescue_total.labels(
					outcome="produced" if ctx.changes else "empty",
				).inc()
			except Exception:
				pass

		# Per-intent defaults backfill. Only runs when
		# ALFRED_PER_INTENT_BUILDERS=1; otherwise a no-op. Fills any missing
		# shape-defining registry fields on dev-mode changesets and annotates
		# ctx.changes[i]["field_defaults_meta"] so the client review UI can
		# render defaults as editable pills. Runs after the rescue path so
		# whichever path produced the changeset gets the same treatment.
		# See docs/specs/2026-04-21-doctype-builder-specialist.md.
		if ctx.changes and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1":
			from alfred.handlers.post_build.backfill_defaults import (
				backfill_defaults_raw,
			)
			try:
				ctx.changes = backfill_defaults_raw(
					ctx.changes,
					module=ctx.module if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1" else None,
				)
			except Exception as e:
				# Safety net: never let backfill crash the pipeline. Log and
				# carry the original changes forward so the user still sees
				# something (even if defaults aren't labelled).
				logger.warning(
					"Defaults backfill failed for conversation=%s: %s",
					ctx.conversation_id, e, exc_info=True,
				)

		# V2: module specialist validation pass. Runs only when both flags
		# on, a module was detected, and we have changes to validate.
		if (
			ctx.changes
			and ctx.module
			and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1"
			and _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1"
		):
			from alfred.agents.specialists.module_specialist import validate_output
			try:
				notes = await validate_output(
					module=ctx.module,
					intent=ctx.intent or "unknown",
					changes=ctx.changes,
					site_config=ctx.conn.site_config or {},
				)
				ctx.module_validation_notes = [n.model_dump() for n in notes]
			except Exception as e:
				logger.warning(
					"validate_output failed for conversation=%s module=%s: %s",
					ctx.conversation_id, ctx.module, e,
				)
				ctx.module_validation_notes = []

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
				"module_validation_notes": ctx.module_validation_notes,
				"detected_module": ctx.module,
			},
		})

	def _send_error_later(self, error: str, code: str, **extra: Any) -> None:
		"""Mark the context as stopped so `run()` emits the error on exit.

		Used from `_phase_post_crew` when the crew completed but produced
		no usable changeset - we want the same error-send shape the outer
		try/except gives us, so we route through the stop signal.
		"""
		self.ctx.stop(error=error, code=code, **extra)
