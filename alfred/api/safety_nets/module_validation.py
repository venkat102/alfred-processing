"""Module-specialist validation pass (V2 + V3) — TD-H1 extraction.

Runs after the changeset is assembled. The primary module keeps full
severity; secondary modules (V3 multi-module mode) get their blockers
capped to ``warning`` so only primary-module notes can gate deploy.
Populates ``ctx.module_validation_notes`` for the preview UI.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.api.pipeline import PipelineContext

logger = logging.getLogger("alfred.safety_nets.module_validation")


async def apply_module_validation(ctx: "PipelineContext") -> None:
	"""Run the module specialist validation pass, populate ``ctx.
	module_validation_notes``.

	No-op when required flags are off, ``ctx.changes`` is empty, or
	``ctx.module`` was never detected. Never raises — the validation
	path is best-effort; on failure we emit empty notes so the
	pipeline keeps flowing.
	"""
	from alfred.config import get_settings
	settings = get_settings()

	if not (
		ctx.changes
		and ctx.module
		and settings.ALFRED_PER_INTENT_BUILDERS
		and settings.ALFRED_MODULE_SPECIALISTS
	):
		return

	from alfred.agents.specialists.module_specialist import (
		cap_secondary_severity,
		validate_output,
	)
	try:
		primary_notes = await validate_output(
			module=ctx.module,
			intent=ctx.intent or "unknown",
			changes=ctx.changes,
			site_config=ctx.conn.site_config or {},
		)
		secondary_notes: list = []
		if settings.ALFRED_MULTI_MODULE:
			for m in ctx.secondary_modules:
				try:
					notes = await validate_output(
						module=m,
						intent=ctx.intent or "unknown",
						changes=ctx.changes,
						site_config=ctx.conn.site_config or {},
					)
					secondary_notes.extend(cap_secondary_severity(notes))
				except Exception as e:  # noqa: BLE001 — per-module best-effort
					logger.warning(
						"secondary validate for %s failed: %s", m, e,
					)
		all_notes = primary_notes + secondary_notes
		ctx.module_validation_notes = [n.model_dump() for n in all_notes]
	except Exception as e:  # noqa: BLE001 — safety net, never block pipeline
		logger.warning(
			"validate_output failed for conversation=%s module=%s: %s",
			ctx.conversation_id, ctx.module, e,
		)
		ctx.module_validation_notes = []
