"""Agent pipeline orchestrator package (TD-H2 PR 3 split from
``alfred/api/pipeline.py``).

Wraps the previously-monolithic ``_run_agent_pipeline`` as an explicit
linear state machine over a shared ``PipelineContext``. Each phase is
a named method on a mixin class that reads + mutates the context; the
orchestrator iterates them in order and auto-wraps each in a tracer
span.

Layout:
  - ``alfred.api.pipeline.context``           — ``StopSignal`` +
    ``PipelineContext`` dataclasses
  - ``alfred.api.pipeline.extractors``        — module-level constants
    + pure helpers (drift, target-doctype extraction, warmup cache)
  - ``alfred.api.pipeline._phases_setup``     — sanitize / load_state
    / warmup / plan_check
  - ``alfred.api.pipeline._phases_orchestrate`` — orchestrate +
    chat/insights/plan short-circuits
  - ``alfred.api.pipeline._phases_dev``       — classify_intent /
    classify_module / provide_module_context / enhance / clarify /
    inject_kb / resolve_mode
  - ``alfred.api.pipeline._phases_build``     — build_crew / run_crew
    / post_crew
  - ``alfred.api.pipeline.runner``            — ``AgentPipeline``
    class composing all four phase mixins

Public surface preserved: ``from alfred.api.pipeline import X`` keeps
working for every name previously exposed (``AgentPipeline``,
``PipelineContext``, ``StopSignal``, ``_detect_drift``,
``_parse_report_candidate_marker``, ``_WARMUP_CACHE``, the
``tracer`` reference that tests patch, …).
"""

from __future__ import annotations

import logging

from alfred.obs import tracer  # noqa: F401 — re-exported for tests that patch alfred.api.pipeline.tracer

logger = logging.getLogger("alfred.pipeline")

# Pure helpers + module constants
from alfred.api.pipeline.extractors import (  # noqa: E402, F401
	_DOCTYPE_NAME_RE,
	_DOCUMENTATION_MODE_PHRASES,
	_ERPNEXT_FIELD_SMELLS,
	_INJECT_MAX_TARGETS,
	_INJECT_SITE_BUDGET,
	_NON_DOCTYPE_CAPITALIZED,
	_PROBE_ATTEMPTS,
	_PROBE_RETRY_BACKOFF_S,
	_REPORT_CANDIDATE_MARKER_RE,
	_WARMUP_CACHE,
	_WARMUP_CACHE_TTL,
	_detect_drift,
	_extract_target_doctypes,
	_parse_report_candidate_marker,
	_render_site_state_block,
	_site_detail_has_artefacts,
	_summarise_probe_error,
)
# Shared dataclasses
from alfred.api.pipeline.context import (  # noqa: E402, F401
	PipelineContext,
	StopSignal,
)
# AgentPipeline class
from alfred.api.pipeline.runner import AgentPipeline  # noqa: E402, F401

__all__ = [
	"AgentPipeline",
	"PipelineContext",
	"StopSignal",
	"tracer",
]
