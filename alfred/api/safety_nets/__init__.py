"""Extracted post-crew safety-net helpers (TD-H1).

Each module exposes a single top-level function that reads and mutates
``ctx`` in place. The pipeline's ``_phase_post_crew`` composes them in
order; nothing here changes behavior relative to the prior inline
block — this is a pure extraction so the concerns become independently
testable and the orchestrator stays readable at a glance.

Why `ctx`-mutation rather than pure functions: every safety net touches
several ``ctx.*`` fields (changes, result_text, module_validation_notes,
report_candidate, intent, ...). Returning new state would require a
ceremonial dance of reassigning fields at each call site; mutating in
place matches the pipeline's existing shape and keeps the orchestrator
to ~50 lines.
"""

from alfred.api.safety_nets.backfill import apply_defaults_backfill
from alfred.api.safety_nets.drift import detect_drift_with_metric
from alfred.api.safety_nets.empty_changeset import emit_empty_changeset_error
from alfred.api.safety_nets.module_validation import apply_module_validation
from alfred.api.safety_nets.report_handoff import apply_report_handoff_safety_net
from alfred.api.safety_nets.rescue import apply_rescue_if_empty

__all__ = [
	"apply_defaults_backfill",
	"apply_module_validation",
	"apply_report_handoff_safety_net",
	"apply_rescue_if_empty",
	"detect_drift_with_metric",
	"emit_empty_changeset_error",
]
