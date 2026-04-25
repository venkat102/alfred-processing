"""Pipeline shared-state dataclasses (TD-H2 PR 3 split from
``alfred/api/pipeline.py``).

``StopSignal`` and ``PipelineContext`` are threaded through every phase
by the runner. Kept in their own module so the phase mixins can
``from alfred.api.pipeline.context import PipelineContext`` for
``self.ctx`` typing without dragging in the AgentPipeline class itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
	from alfred.agents.crew import CrewState
	from alfred.agents.token_tracker import TokenTracker
	from alfred.api.websocket import ConnectionState
	from alfred.state.conversation_memory import ConversationMemory
	from alfred.state.store import StateStore

logger = logging.getLogger("alfred.pipeline")



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

	conn: ConnectionState
	conversation_id: str
	prompt: str

	# Mode selection (three-mode chat feature).
	# manual_mode_override is the user's pick from the chat UI switcher
	# ("auto" | "dev" | "plan" | "insights"). The orchestrator phase
	# reads this, decides a final mode, and writes it to `mode` plus
	# `orchestrator_reason`. Chat/insights/plan modes skip the crew and
	# emit their own reply message types.
	manual_mode_override: str = "auto"
	# When True, bypass the analytics-shape redirect in classify_mode so a
	# user who explicitly clicks "Run in Dev anyway" on the redirect banner
	# gets dev mode on the retry. Frontend sends this as data.force_dev.
	force_dev_override: bool = False
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

	# V3 multi-module additions. Populated only when ALFRED_MULTI_MODULE=1.
	# module_secondary_contexts maps module key -> that module's
	# provide_context snippet so the UI can attribute text to its source.
	secondary_modules: list[str] = field(default_factory=list)
	module_secondary_contexts: dict[str, str] = field(default_factory=dict)

	# V4: structured Insights -> Report handoff payload. When the client
	# injects a __report_candidate__ JSON block into the prompt (user
	# clicked "Save as Report" on an Insights reply), the pipeline parses
	# it here and force-classifies intent=create_report.
	# Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
	report_candidate: dict | None = None

	# Services
	store: StateStore | None = None
	conversation_memory: ConversationMemory | None = None

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
	crew_state: CrewState | None = None
	crew_result: dict | None = None
	# Per-conversation token tally. Populated at the end of ``_phase_run_crew``
	# from each agent's ``_token_process``, then surfaced as a ``usage`` event
	# and (for REST runs) folded into the final task_state. Stays None for
	# chat / insights / plan modes — they don't run the multi-agent crew so
	# there's nothing per-agent to break down.
	token_tracker: TokenTracker | None = None
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


