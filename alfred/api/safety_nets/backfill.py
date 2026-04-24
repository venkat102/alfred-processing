"""Per-intent registry defaults backfill (TD-H1 extraction).

Runs when ``ALFRED_PER_INTENT_BUILDERS=1``; fills any missing shape-
defining fields from the intent registry and annotates ``ctx.changes[i]
["field_defaults_meta"]`` so the client review UI can render defaults
as editable pills. When ``ALFRED_MODULE_SPECIALISTS=1`` the primary
module is passed for module-specific defaults; when
``ALFRED_MULTI_MODULE=1`` secondary modules come along for the ride.

See docs/specs/2026-04-21-doctype-builder-specialist.md for the full
backfill contract.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.api.pipeline import PipelineContext

logger = logging.getLogger("alfred.safety_nets.backfill")


def apply_defaults_backfill(ctx: "PipelineContext") -> None:
	"""Backfill registry defaults into ``ctx.changes`` in place.

	No-op when ``ALFRED_PER_INTENT_BUILDERS`` is off or ``ctx.changes``
	is empty. Never raises — backfill failures are swallowed with a
	warning so the user still sees *something* in the preview even if
	defaults aren't labelled.
	"""
	from alfred.config import get_settings
	settings = get_settings()

	if not ctx.changes or not settings.ALFRED_PER_INTENT_BUILDERS:
		return

	from alfred.handlers.post_build.backfill_defaults import (
		backfill_defaults_raw,
	)
	try:
		module_arg = (
			ctx.module
			if settings.ALFRED_MODULE_SPECIALISTS
			else None
		)
		secondary_arg = (
			ctx.secondary_modules
			if settings.ALFRED_MULTI_MODULE
			else []
		)
		ctx.changes = backfill_defaults_raw(
			ctx.changes,
			module=module_arg,
			secondary_modules=secondary_arg,
		)
	except Exception as e:  # noqa: BLE001 — safety net, log and carry on
		logger.warning(
			"Defaults backfill failed for conversation=%s: %s",
			ctx.conversation_id, e, exc_info=True,
		)
