"""Drift detection for the post-crew phase (TD-H1 extraction).

qwen2.5-coder:32b on Ollama sometimes slips out of the task structure
and regurgitates training-data Frappe docs. ``_detect_drift`` in
``alfred.api.pipeline`` catches the common shapes (DocType-name prose,
ERPNext field smells, doc-mode giveaway phrases). This helper wraps
the detector + the Prometheus counter so the post-crew orchestrator
stays short.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.api.pipeline import PipelineContext

logger = logging.getLogger("alfred.safety_nets.drift")


def detect_drift_with_metric(ctx: PipelineContext) -> str | None:
	"""Run drift detection on ``ctx.result_text`` and record the metric.

	Returns the reason string when drift is detected, else None. When
	drift is detected, increments ``crew_drift_total{reason=...}`` so
	the framework-quirk tax is quantifiable over time. Behavior is
	byte-for-byte identical to the inline block the pipeline used to
	carry (see TD-H1 for the extraction rationale).
	"""
	# Late import to avoid a circular import at module load — pipeline
	# imports this module, and this function needs pipeline's private
	# ``_detect_drift`` helper.
	from alfred.api.pipeline import _detect_drift

	reason = _detect_drift(ctx.result_text, ctx.prompt)
	if not reason:
		return None

	try:
		from alfred.obs.metrics import crew_drift_total
		crew_drift_total.labels(reason=reason).inc()
	except Exception:  # noqa: BLE001 — metrics must never block the pipeline
		pass

	return reason
