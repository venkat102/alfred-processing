"""Empty-changeset error emission (TD-H1 extraction).

When extraction + rescue both returned nothing, we send a specific
error to the UI. The message depends on whether the cause was
drift-induced (the model regurgitated training-data prose) or a
generic extraction miss — different phrasing guides the user's next
rephrasing attempt.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	pass

logger = logging.getLogger("alfred.safety_nets.empty_changeset")


def emit_empty_changeset_error(
	pipeline,
	drift_reason: str | None,
) -> bool:
	"""If ``ctx.changes`` is empty, schedule the EMPTY_CHANGESET error
	and return True so the caller can early-exit the phase.

	Takes the pipeline (rather than just ctx) because the error
	emission goes through ``pipeline._send_error_later`` which ties
	into the outer ``run()`` error boundary. Returns False when
	``ctx.changes`` is non-empty (no-op).
	"""
	ctx = pipeline.ctx
	if ctx.changes:
		return False

	logger.warning(
		"Pipeline completed but extraction + rescue both returned "
		"empty. Drift=%s. Result text (first 500): %r",
		drift_reason or "no",
		ctx.result_text[:500],
	)
	if drift_reason:
		reason_slug = "drift_detected"
		user_message = (
			f"Alfred's output was off-topic ({drift_reason}). "
			"The rescue path also couldn't produce a valid changeset. "
			"Please rephrase with the exact DocType name and the exact "
			"rule, e.g. \"On Employee DocType, before insert, throw an "
			"error if age is less than 24.\""
		)
	else:
		reason_slug = "agent_returned_text"
		user_message = (
			"Alfred couldn't turn this request into a deployable change. "
			"The agent produced text instead of a structured changeset, "
			"which usually means the customization type isn't supported "
			"yet or the request needs rephrasing. Try restating with the "
			"exact DocType name and action, or check the docs for "
			"supported capabilities."
		)
	agent_output_preview = (ctx.result_text or "").strip()[:400]
	pipeline._send_error_later(
		user_message,
		"EMPTY_CHANGESET",
		drift_reason=drift_reason or "",
		reason=reason_slug,
		agent_output_preview=agent_output_preview,
	)
	return True
