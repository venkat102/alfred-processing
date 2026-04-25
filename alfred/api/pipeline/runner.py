"""``AgentPipeline`` orchestrator (TD-H2 PR 3 split from
``alfred/api/pipeline.py``).

Why this shape:
  - Each phase is independently testable. Future unit tests can build a
    ``PipelineContext`` directly, call one phase method, and assert on the
    resulting state without booting the whole pipeline.
  - Adding a new phase is two small edits: add the method on a mixin,
    add it to the ``PHASES`` list. No surgery in the middle of a 400-line
    function.
  - Observability is free — one ``async with tracer.span(f"pipeline.{name}")``
    in ``run()`` covers every phase automatically.
  - Error boundaries are centralized: ``run()`` catches TimeoutError /
    generic exceptions once and emits the same user-visible error shape
    the old code did, regardless of which phase failed.

The phase methods themselves live in mixin modules under this package
(``_phases_setup``, ``_phases_orchestrate``, ``_phases_dev``,
``_phases_build``) so each file stays under the TD-H2 800-LOC target.
``AgentPipeline`` inherits from all four mixins; method resolution
falls through to the mixin that defines each ``_phase_X``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import WebSocketDisconnect

from alfred.api.pipeline._phases_build import _PhasesBuildMixin
from alfred.api.pipeline._phases_dev import _PhasesDevMixin
from alfred.api.pipeline._phases_orchestrate import _PhasesOrchestrateMixin
from alfred.api.pipeline._phases_setup import _PhasesSetupMixin
from alfred.api.pipeline.context import PipelineContext

logger = logging.getLogger("alfred.pipeline")


class AgentPipeline(
	_PhasesSetupMixin,
	_PhasesOrchestrateMixin,
	_PhasesDevMixin,
	_PhasesBuildMixin,
):
	"""Linear orchestrator over `PipelineContext`.

	Phase methods are resolved by name from `PHASES`. To add a phase: add a
	method `async def _my_phase(self):` on the appropriate mixin, append
	its name to `PHASES`.
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

		# Resolve `tracer` through the package so tests that
		# ``patch("alfred.api.pipeline.tracer", ...)`` affect this lookup.
		# A direct ``from alfred.obs import tracer`` at module scope
		# would bypass the patch.
		from alfred.api import pipeline as _pkg
		from alfred.obs.metrics import pipeline_phase_duration_seconds
		tracer = _pkg.tracer

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
		except TimeoutError:
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
			except (RuntimeError, WebSocketDisconnect, OSError) as e:
				# WS already closed (RuntimeError), client gone
				# (WebSocketDisconnect), or socket died (OSError). The
				# cancellation still took effect server-side.
				logger.warning("Failed to send cancellation message: %s", e)
			return
		try:
			await self.ctx.conn.send({
				"msg_id": str(uuid.uuid4()),
				"type": "error",
				"data": {"error": error, "code": code, **extra},
			})
		except (RuntimeError, WebSocketDisconnect, OSError) as e:
			# Same shape as the cancel path above.
			logger.warning("Failed to send error message: %s", e)

	async def _save_memory_with_feedback(self) -> None:
		"""Persist conversation memory and surface failures to the user.

		Ported from master commit f8b0810. All four chat / insights /
		plan / dev flows call this at the end of their turn. Redis being
		unreachable or a serialisation glitch must not crash the phase
		nor silently erase follow-up context — log, send a non-blocking
		info event so the user knows, and return normally so the primary
		output reaches them.

		No-ops when ``store`` or ``conversation_memory`` is None. Info
		event uses the same ``{type: info, data: {code, message}}``
		shape as CLARIFIER_LATE_RESPONSE (master 31047c3).
		"""
		ctx = self.ctx
		if ctx.conversation_memory is None or ctx.store is None:
			return

		from alfred.state.conversation_memory import save_conversation_memory

		try:
			await save_conversation_memory(
				ctx.store, ctx.conn.site_id, ctx.conversation_id,
				ctx.conversation_memory,
			)
		except Exception as e:  # noqa: BLE001 — store-boundary best-effort: we deliberately surface every failure as a user-visible info toast rather than crashing the phase
			logger.warning(
				"conversation memory save failed for %s@%s conversation=%s: %s",
				ctx.conn.user, ctx.conn.site_id, ctx.conversation_id, e,
			)
			try:
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "info",
					"data": {
						"message": (
							"Heads up: conversation memory couldn't be saved. "
							"Follow-up prompts in this conversation may not "
							"recall this turn's context."
						),
						"code": "MEMORY_SAVE_FAILED",
					},
				})
			except (RuntimeError, WebSocketDisconnect, OSError) as send_err:
				# WS also down — out of user-facing options.
				logger.debug(
					"MEMORY_SAVE_FAILED info send also failed: %s", send_err,
				)
