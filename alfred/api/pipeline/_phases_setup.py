"""Setup-phase mixin: sanitize, load_state, warmup, plan_check.

TD-H2 PR 3 split from ``alfred/api/pipeline.py``. Mixed into
``AgentPipeline`` via ``runner.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

import redis.asyncio as aioredis
from fastapi import WebSocketDisconnect

from alfred.config import get_settings as _get_settings
from alfred.api.pipeline.extractors import (
	_PROBE_ATTEMPTS,
	_PROBE_RETRY_BACKOFF_S,
	_WARMUP_CACHE,
	_WARMUP_CACHE_TTL,
	_summarise_probe_error,
)

if TYPE_CHECKING:
	from alfred.api.pipeline.context import PipelineContext

logger = logging.getLogger("alfred.pipeline")


class _PhasesSetupMixin:
	"""Setup phases — input safety, state recovery, model warmup, plan check."""

	# Set on the concrete AgentPipeline class via the runner.
	ctx: "PipelineContext"

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

		all_models: set[tuple[str, str]] = set()
		for tier in (TIER_TRIAGE, TIER_REASONING, TIER_AGENT):
			model, base_url, _ = _resolve_ollama_config_for_tier(site_config, tier)
			if model.startswith("ollama/"):
				all_models.add((model.removeprefix("ollama/"), base_url))

		if not all_models:
			# Cloud-only configuration - nothing to probe.
			return

		# Skip probes for any (model, url) we talked to successfully within
		# TTL. Transient Ollama reloads commonly clear within a few seconds,
		# so re-probing on every prompt is pure overhead once we've seen it
		# respond once.
		now = time.monotonic()
		cache_hits: list[tuple[str, str]] = []
		models_to_probe: set[tuple[str, str]] = set()
		for m, u in all_models:
			last_ok = _WARMUP_CACHE.get((m, u))
			if last_ok is not None and (now - last_ok) < _WARMUP_CACHE_TTL:
				cache_hits.append((m, u))
			else:
				models_to_probe.add((m, u))

		if cache_hits and not models_to_probe:
			logger.info(
				"Warmup cache hit for all %d model(s): %s",
				len(cache_hits), ", ".join(m for m, _ in cache_hits),
			)
			return
		if cache_hits:
			logger.info(
				"Warmup cache hit for %d of %d model(s): %s",
				len(cache_hits), len(all_models),
				", ".join(m for m, _ in cache_hits),
			)

		async def _do_probe(ollama_model: str, base_url: str):
			# SSRF gate: the probe runs on every pipeline warmup, making
			# it a parallel attack surface to ollama_chat. Validate the
			# URL here too. See alfred/security/url_allowlist.py.
			from alfred.security.url_allowlist import (
				SsrfPolicyError, validate_llm_url,
			)
			try:
				validate_llm_url(base_url)
			except SsrfPolicyError as e:
				# Raise with a clear message; the outer _probe_one loop
				# logs + records the failure per attempt.
				raise RuntimeError(
					f"Probe URL rejected by SSRF policy ({e.reason}): {e}"
				) from e
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

		async def _probe_one(ollama_model: str, base_url: str):
			"""Retry-with-backoff wrapper around a single probe.

			Two attempts with a short sleep between absorbs the 3-8s
			window Ollama takes to swap a model back into VRAM after an
			idle gap. Truly-dead Ollama still fails the second attempt
			and surfaces the underlying exception.
			"""
			last_exc: Exception | None = None
			for attempt in range(_PROBE_ATTEMPTS):
				try:
					await _do_probe(ollama_model, base_url)
					return
				except (OSError, RuntimeError) as exc:
					# OSError covers urllib URLError/HTTPError/TimeoutError
					# (all OSError subclasses in Py 3.3+) plus raw socket
					# failure. RuntimeError covers the SSRF-policy reject
					# _do_probe raises for a bad base_url. Anything else
					# is a logic bug — propagate.
					last_exc = exc
					if attempt + 1 < _PROBE_ATTEMPTS:
						logger.info(
							"Warmup probe attempt %d/%d failed for %s: %s. "
							"Retrying in %.1fs.",
							attempt + 1, _PROBE_ATTEMPTS,
							ollama_model, _summarise_probe_error(exc),
							_PROBE_RETRY_BACKOFF_S,
						)
						try:
							from alfred.obs.metrics import llm_errors_total
							llm_errors_total.labels(
								tier="warmup", error_type="probe_retry",
							).inc()
						except Exception:  # noqa: BLE001 — metrics best-effort; retry path must not crash on a broken metrics import
							pass
						await asyncio.sleep(_PROBE_RETRY_BACKOFF_S)
			assert last_exc is not None
			raise last_exc

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
					"Ollama health probe failed for %s at %s after %d "
					"attempt(s): %s",
					model, base_url, _PROBE_ATTEMPTS, reason,
				)
				failures.append((model, base_url, reason))
				# Evict so the next prompt re-probes instead of trusting a
				# stale ok stamp. Safe if the tuple was never in the cache.
				_WARMUP_CACHE.pop((model, base_url), None)
			else:
				warmed.append(model)
				_WARMUP_CACHE[(model, base_url)] = time.monotonic()

		if failures:
			from alfred.obs.metrics import llm_errors_total

			# Record each distinct failure as an llm_errors_total dimension so
			# the rate of OLLAMA_UNHEALTHY is visible on the /metrics scrape.
			for _, _, reason in failures:
				llm_errors_total.labels(
					tier="warmup", error_type="OLLAMA_UNHEALTHY",
				).inc()
			failure_list = ", ".join(
				f"{m} ({r})" for m, _, r in failures
			)
			ctx.stop(
				error=(
					f"Processing service is unavailable: Ollama did not "
					f"respond after {_PROBE_ATTEMPTS} probe attempts "
					f"({_PROBE_RETRY_BACKOFF_S:g}s apart). Failed "
					f"model(s): {failure_list}. Check that Ollama is "
					f"running (`ollama ps`) and that all tier models are "
					"loaded. Contact your admin if the issue persists."
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
		except Exception as e:  # noqa: BLE001 — defensive wrapper; AdminClient.check_plan catches its own httpx/OSError internally and returns a dict-shape result even on failure, but result-dict access (KeyError, TypeError) or admin-client logic bugs must not block the pipeline
			logger.warning("Plan check failed (allowing by default): %s", e)

