"""Rescue regeneration — runs when the Developer drifted into prose
(TD-H1 extraction).

When extraction returns an empty changeset, we take one more shot at
producing something usable by calling a focused LLM path on the
original prompt. Records whether the rescue actually produced a
changeset via ``crew_rescue_total{outcome=produced|empty}`` so we can
tell whether rescue is earning its keep vs burning tokens on lost
causes.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.api.pipeline import PipelineContext

logger = logging.getLogger("alfred.safety_nets.rescue")


async def apply_rescue_if_empty(
	ctx: PipelineContext,
	drift_reason: str | None,
) -> None:
	"""If ``ctx.changes`` is empty, attempt a rescue regeneration and
	mutate ``ctx.changes`` with the result.

	No-op when ``ctx.changes`` is already non-empty. The rescue call
	itself lives in ``alfred.api.websocket`` (``_rescue_regenerate_
	changeset``); we only own the decision to invoke it and the metric.
	"""
	if ctx.changes:
		return

	# Late import: websocket → pipeline → safety_nets → websocket cycle
	# is broken by the import happening at call time.
	from alfred.api.websocket import _rescue_regenerate_changeset

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

	try:
		from alfred.obs.metrics import crew_rescue_total
		crew_rescue_total.labels(
			outcome="produced" if ctx.changes else "empty",
		).inc()
	except Exception:  # noqa: BLE001 — metrics must never block the pipeline
		pass
