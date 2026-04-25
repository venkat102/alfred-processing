"""Dev-mode phases mixin: classify_intent, classify_module, provide_module_context, enhance, clarify, inject_kb, resolve_mode.

TD-H2 PR 3 split from ``alfred/api/pipeline.py``. Mixed into
``AgentPipeline`` via ``runner.py``.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from fastapi import WebSocketDisconnect

from alfred.api.pipeline.extractors import (
	_INJECT_SITE_BUDGET,
	_extract_target_doctypes,
	_parse_report_candidate_marker,
	_render_site_state_block,
	_site_detail_has_artefacts,
)
from alfred.config import get_settings as _get_settings
from alfred.obs import tracer

if TYPE_CHECKING:
	from alfred.api.pipeline.context import PipelineContext

logger = logging.getLogger("alfred.pipeline")


class _PhasesDevMixin:
	"""Dev-mode phases — per-intent + per-module classification plus enhance/clarify/inject_kb/resolve_mode."""

	# Set on the concrete AgentPipeline class via the runner.
	ctx: PipelineContext

	async def _phase_classify_intent(self) -> None:
		"""Classify the dev-mode prompt into a Builder specialist intent.

		No-op for non-dev modes and when ALFRED_PER_INTENT_BUILDERS is unset
		(``_phase_build_crew`` and the backfill in ``_phase_post_crew`` are
		also flag-gated, so an off flag means zero behavioural change from
		pre-feature Alfred). Stores the IntentDecision fields on ctx.intent*
		for downstream phases to read.

		See docs/specs/2026-04-21-doctype-builder-specialist.md.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		settings = _get_settings()
		if not settings.ALFRED_PER_INTENT_BUILDERS:
			return

		# V4 handoff short-circuit: when the client's prompt carries a
		# __report_candidate__ JSON block (user clicked "Save as Report"
		# on an Insights reply), parse it, stash on ctx, and force-classify
		# intent=create_report. Avoids a second heuristic/LLM pass - the
		# Insights handler already did the interpretation work.
		if settings.ALFRED_REPORT_HANDOFF:
			parsed = _parse_report_candidate_marker(ctx.prompt)
			if parsed is not None:
				ctx.report_candidate = parsed
				ctx.intent = "create_report"
				ctx.intent_source = "handoff"
				ctx.intent_confidence = "high"
				ctx.intent_reason = "__report_candidate__ marker present"
				logger.info(
					"Intent handoff for conversation=%s: intent=create_report "
					"source=handoff candidate_keys=%s",
					ctx.conversation_id, list(parsed.keys()),
				)
				return

		from alfred.orchestrator import classify_intent

		decision = await classify_intent(
			prompt=ctx.prompt,
			site_config=ctx.conn.site_config or {},
		)
		ctx.intent = decision.intent
		ctx.intent_source = decision.source
		ctx.intent_confidence = decision.confidence
		ctx.intent_reason = decision.reason

		logger.info(
			"Intent decision for conversation=%s: intent=%s source=%s confidence=%s reason=%r",
			ctx.conversation_id, decision.intent, decision.source,
			decision.confidence, decision.reason,
		)

	async def _phase_classify_module(self) -> None:
		"""Classify the dev-mode prompt's target module for specialist selection.

		No-op for non-dev modes, when ALFRED_PER_INTENT_BUILDERS is off, or
		when ALFRED_MODULE_SPECIALISTS is off. Stores the ModuleDecision
		fields on ctx.module* for downstream phases to read.

		See docs/specs/2026-04-22-module-specialists.md.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		settings = _get_settings()
		if not settings.ALFRED_PER_INTENT_BUILDERS:
			return
		if not settings.ALFRED_MODULE_SPECIALISTS:
			return

		# Heuristic: use the first extracted target DocType so module
		# detection can take the high-confidence path (target_doctype
		# match) rather than falling back to keyword hints.
		targets = _extract_target_doctypes(ctx.prompt)
		first_target = targets[0] if targets else None
		ctx.module_target_doctype = first_target

		if settings.ALFRED_MULTI_MODULE:
			# V3: primary + secondaries
			from alfred.orchestrator import detect_modules
			multi = await detect_modules(
				prompt=ctx.prompt,
				target_doctype=first_target,
				site_config=ctx.conn.site_config or {},
			)
			ctx.module = multi.module
			ctx.secondary_modules = multi.secondary_modules
			ctx.module_source = multi.source
			ctx.module_confidence = multi.confidence
			ctx.module_reason = multi.reason
			logger.info(
				"Multi-module decision for conversation=%s: primary=%s "
				"secondaries=%s source=%s confidence=%s reason=%r",
				ctx.conversation_id, multi.module, multi.secondary_modules,
				multi.source, multi.confidence, multi.reason,
			)
			return

		# V2: single-module path (unchanged)
		from alfred.orchestrator import detect_module
		decision = await detect_module(
			prompt=ctx.prompt,
			target_doctype=first_target,
			site_config=ctx.conn.site_config or {},
		)
		ctx.module = decision.module
		ctx.module_source = decision.source
		ctx.module_confidence = decision.confidence
		ctx.module_reason = decision.reason

		logger.info(
			"Module decision for conversation=%s: module=%s source=%s confidence=%s reason=%r",
			ctx.conversation_id, decision.module, decision.source,
			decision.confidence, decision.reason,
		)

	async def _phase_provide_module_context(self) -> None:
		"""Invoke module specialist's context pass; stash snippet on ctx.

		No-op when flags off or when no module was detected.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		settings = _get_settings()
		if not settings.ALFRED_PER_INTENT_BUILDERS:
			return
		if not settings.ALFRED_MODULE_SPECIALISTS:
			return
		if not ctx.module:
			return

		from alfred.agents.specialists.module_specialist import (
			provide_context,
			provide_family_context,
		)
		from alfred.registry.module_loader import ModuleRegistry

		# Surface the Redis client (if configured) to the specialist so
		# the 5-min context cache is shared across workers. When Redis is
		# unreachable or not configured, the specialist falls back to a
		# process-local cache automatically.
		redis = getattr(getattr(ctx.conn, "websocket", None), "app", None)
		redis = getattr(getattr(redis, "state", None), "redis", None)

		registry = ModuleRegistry.load()

		def _display(m: str) -> str:
			try:
				return registry.get(m).get("display_name", m)
			except KeyError:
				# ModuleRegistry raises UnknownModuleError (KeyError) for
				# missing entries. Fall back to the raw name.
				return m

		def _family_display(f: str) -> str:
			try:
				return registry.get_family(f).get("display_name", f)
			except KeyError:
				# UnknownFamilyError inherits from KeyError; same pattern.
				return f

		# Primary context call (same as V2 path)
		try:
			primary_ctx = await provide_context(
				module=ctx.module,
				intent=ctx.intent or "unknown",
				target_doctype=ctx.module_target_doctype,
				site_config=ctx.conn.site_config or {},
				redis=redis,
			)
		except Exception as e:  # noqa: BLE001 — defensive wrapper; provide_context has its own broad catch (LLM + cache + registry layer; heterogeneous). Empty context is the safe degraded mode.
			logger.warning(
				"provide_module_context failed for conversation=%s module=%s: %s",
				ctx.conversation_id, ctx.module, e,
			)
			primary_ctx = ""

		# Primary family context. Families group related modules (e.g.
		# accounts+selling+buying under Transactions) and carry
		# cross-module invariants that apply to every member. We fetch
		# the family snippet once per conversation and prepend it above
		# the PRIMARY MODULE section. ``custom`` is familyless by
		# design - skip silently.
		primary_family = registry.family_for_module(ctx.module) if ctx.module else None
		primary_family_ctx = ""
		if primary_family:
			try:
				primary_family_ctx = await provide_family_context(
					family=primary_family,
					intent=ctx.intent or "unknown",
					site_config=ctx.conn.site_config or {},
					redis=redis,
				)
			except Exception as e:  # noqa: BLE001 — defensive wrapper; provide_family_context has its own broad catch, same shape as provide_context above.
				logger.warning(
					"provide_family_context failed for conversation=%s family=%s: %s",
					ctx.conversation_id, primary_family, e,
				)

		# V3: secondary context calls (silent failure each)
		secondary_ctxs: dict[str, str] = {}
		if settings.ALFRED_MULTI_MODULE:
			for m in ctx.secondary_modules:
				try:
					snippet = await provide_context(
						module=m,
						intent=ctx.intent or "unknown",
						target_doctype=ctx.module_target_doctype,
						site_config=ctx.conn.site_config or {},
						redis=redis,
					)
					if snippet:
						secondary_ctxs[m] = snippet
				except Exception as e:  # noqa: BLE001 — defensive wrapper; provide_context has its own broad catch (LLM + cache + registry layer; heterogeneous). Empty context is the safe degraded mode.
					logger.warning(
						"secondary provide_context failed for %s: %s", m, e,
					)

		# V2 path: bare primary snippet, no header wrapper. V3 path:
		# labeled PRIMARY FAMILY / PRIMARY MODULE / SECONDARY MODULE
		# sections so the LLM can distinguish cross-module invariants
		# from module-specific conventions from advisory context.
		# Header wrapping only applies when V3 flag is on - V2-only
		# runs keep their existing prompt shape.
		if settings.ALFRED_MULTI_MODULE:
			parts: list[str] = []
			if primary_family and primary_family_ctx:
				parts.append(
					f"PRIMARY FAMILY ({_family_display(primary_family)}):\n{primary_family_ctx}"
				)
			if primary_ctx:
				parts.append(f"PRIMARY MODULE ({_display(ctx.module)}):\n{primary_ctx}")
			elif ctx.secondary_modules:
				parts.append(f"PRIMARY MODULE ({_display(ctx.module)}): (no context)")
			# Family dedupe: a secondary module in the SAME family as the
			# primary doesn't re-emit a family section - the family-level
			# invariants were already surfaced above. We just label the
			# module snippet as SECONDARY MODULE.
			for m, s in secondary_ctxs.items():
				parts.append(f"SECONDARY MODULE CONTEXT ({_display(m)}):\n{s}")
			ctx.module_context = "\n\n".join(parts) if parts else primary_ctx
		else:
			# V2 fallback: prepend family snippet when available so V2-only
			# callers also benefit from cross-module invariants. Keeps the
			# bare-snippet shape for modules that have no family (custom).
			if primary_family and primary_family_ctx and primary_ctx:
				ctx.module_context = (
					f"FAMILY CONTEXT ({_family_display(primary_family)}): "
					f"{primary_family_ctx}\n\n{primary_ctx}"
				)
			else:
				ctx.module_context = primary_ctx
		ctx.module_secondary_contexts = secondary_ctxs

	async def _phase_enhance(self) -> None:
		if self.ctx.mode != "dev":
			# Enhance is a Dev-mode concern. Plan/Insights/Chat have their
			# own entry paths.
			return

		from alfred.agents.prompt_enhancer import enhance_prompt

		ctx = self.ctx
		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_status",
			"data": {
				"agent": "System", "status": "enhancing",
				"message": "Analyzing your request...",
			},
		})

		# Phase C: Plan -> Dev handoff. If the conversation has an active
		# plan doc (from a prior Plan-mode run) and the current prompt
		# looks like an approval, flip the plan's status to "approved"
		# BEFORE rendering the memory context block so `render_for_prompt`
		# emits the full step list (only approved plans render their
		# steps verbatim - see conversation_memory.render_for_prompt).
		# Cross-mixin call — _maybe_approve_active_plan lives on
		# _PhasesOrchestrateMixin; resolved via AgentPipeline's MRO.
		self._maybe_approve_active_plan()  # type: ignore[attr-defined]

		conversation_context = (
			ctx.conversation_memory.render_for_prompt()
			if ctx.conversation_memory else ""
		)

		ctx.enhanced_prompt = await enhance_prompt(
			ctx.prompt, ctx.user_context, ctx.conn.site_config,
			conversation_context=conversation_context or None,
		)

		# Bind the early event callback so the clarifier (next phase) can
		# stream updates before the full event_callback is set up.
		async def _early_cb(event_type: str, data: dict) -> None:
			try:
				await ctx.conn.send({
					"msg_id": str(uuid.uuid4()),
					"type": "agent_status",
					"data": {"event": event_type, **data},
				})
			except (RuntimeError, WebSocketDisconnect, OSError) as e:
				# RuntimeError: WS closed under us. WebSocketDisconnect:
				# client went away. OSError: underlying socket error.
				# Clarifier may still be streaming when the client drops.
				logger.warning("early event_callback send failed: %s", e)

		ctx.early_event_callback = _early_cb

	async def _phase_clarify(self) -> None:
		if self.ctx.mode != "dev":
			return

		from alfred.api.websocket import _clarify_requirements

		ctx = self.ctx
		ctx.enhanced_prompt, ctx.clarify_qa_pairs = await _clarify_requirements(
			ctx.enhanced_prompt, ctx.conn, ctx.early_event_callback
		)
		if ctx.clarify_qa_pairs and ctx.conversation_memory is not None:
			ctx.conversation_memory.add_clarifications(ctx.clarify_qa_pairs)

	async def _phase_inject_kb(self) -> None:
		"""Auto-inject Frappe KB entries + site state into the enhanced prompt.

		Two retrievals, one combined banner:
		  (1) FKB keyword search (Phase B) - platform rules, APIs, idioms
		      matching the user's enhanced prompt.
		  (2) Site reconnaissance (Phase B.5) - for each DocType named in the
		      prompt, fetch existing artefacts (workflows, server scripts,
		      custom fields, notifications, client scripts) via the
		      get_site_customization_detail MCP tool.

		Both retrievals run off the same source-of-truth (the user's enhanced
		prompt, BEFORE we prepend anything). Their renderings are concatenated
		into a single banner:

		    === FRAPPE KB CONTEXT ===
		    [rule/api/idiom entries]
		    ==========================

		    === SITE STATE FOR "Employee" ===
		    [existing artefacts on the target DocType]
		    ==================================

		    --- USER REQUEST ---
		    [original enhanced prompt]

		Skips when mode != "dev" / stop signal set / no MCP client / empty
		prompt. Individual retrievals fail open - if FKB search errors out,
		site-state still injects; if site-detail fails for one target, the
		other target still gets recon. A total failure just leaves the
		enhanced_prompt unchanged and lets the crew run with the pre-Phase-E
		hardcoded rule blocks.
		"""
		ctx = self.ctx
		span = tracer.current()

		if ctx.mode != "dev":
			if span: span.set(skipped="not-dev-mode")
			return
		if ctx.should_stop:
			if span: span.set(skipped="stop-signal")
			return
		if not ctx.enhanced_prompt:
			if span: span.set(skipped="empty-prompt")
			logger.debug("inject_kb: empty enhanced_prompt - skipping")
			return
		# Note: no MCP-client short-circuit. FKB retrieval is local; only
		# site-recon needs MCP. The site-recon block below guards itself.

		# Snapshot the source prompt BEFORE we prepend anything. Target
		# DocType extraction and FKB search both run off this snapshot so
		# neither sees doctype-looking names we add in the banners.
		source_prompt = ctx.enhanced_prompt

		# ── (1) FKB hybrid search (local, no MCP round-trip) ──────────
		# Phase C: retrieval moved from the Frappe-side MCP tool to the
		# processing-local `alfred.knowledge.fkb` module so we can layer
		# semantic (sentence-transformers) on top of keyword without
		# installing ML deps in the bench venv. The module reads the same
		# YAML source-of-truth and falls back to keyword-only if the model
		# can't load (e.g. missing weights, import error) - the pipeline
		# keeps running either way.
		from alfred.knowledge import fkb as _fkb

		fkb_rendered: list[str] = []
		try:
			fkb_hits = _fkb.search_hybrid(source_prompt, k=3)
		except Exception as e:  # noqa: BLE001 — 3rd-party ML boundary (sentence-transformers / torch). Raises a wide zoo (OOM, CUDA, safetensors, import-time). Fail open to keyword-only.
			if span: span.set(fkb_error=f"fkb:{type(e).__name__}")
			logger.warning("inject_kb: FKB search failed: %s", e)
			fkb_hits = []

		for entry in fkb_hits:
			if not isinstance(entry, dict):
				continue
			entry_id = entry.get("id") or "<unknown>"
			kind = entry.get("kind") or "entry"
			title = (entry.get("title") or "").strip()
			summary = (entry.get("summary") or "").strip()
			body = (entry.get("body") or "").strip()
			# `_mode` is "keyword" or "semantic" - include it in the block
			# header so the agent (and traces) can see which retriever
			# surfaced each entry.
			mode_tag = entry.get("_mode", "kb")

			block_parts = [f"[{kind}: {entry_id} via {mode_tag}] {title}"]
			if summary:
				block_parts.append(f"Summary: {summary}")
			if body:
				block_parts.append(body)
			fkb_rendered.append("\n".join(block_parts))
			ctx.injected_kb.append(entry_id)

		# ── (2) Site reconnaissance (requires MCP) ────────────────────
		# Unlike FKB (local YAML), site-recon queries the live Frappe app
		# via MCP. Skip cleanly if no MCP client is wired - FKB still
		# injects.
		site_rendered: list[str] = []
		site_artefact_count = 0
		if ctx.conn.mcp_client:
			targets = _extract_target_doctypes(source_prompt)
			# Per-DocType char budget - split the total budget evenly so one
			# heavily-customized DocType can't starve the other.
			per_target_budget = (
				_INJECT_SITE_BUDGET // max(len(targets), 1) if targets else 0
			)
			for doctype in targets:
				try:
					detail = await ctx.conn.mcp_client.call_tool(
						"get_site_customization_detail",
						{"doctype": doctype},
					)
				except Exception as e:  # noqa: BLE001 — MCP tool boundary (HTTP/JSON-RPC to external Frappe app). Transport, server-side, and protocol errors all surface here; fail-open per-target keeps inject_kb degraded-but-alive.
					logger.warning(
						"inject_kb: site-detail call failed for %r: %s", doctype, e,
					)
					continue
				if not isinstance(detail, dict) or detail.get("error"):
					# Unknown DocType, permission denied, or infra error - silently
					# skip. Don't treat "not_found" as a real failure; it's the
					# common case for targets extracted from prompt noise.
					continue
				if not _site_detail_has_artefacts(detail):
					# DocType exists but has no customizations on this site - no
					# point injecting a block that says "nothing to see here".
					continue

				ctx.injected_site_state[doctype] = detail
				site_rendered.append(
					_render_site_state_block(doctype, detail, per_target_budget)
				)
				site_artefact_count += sum(
					len(detail.get(k) or [])
					for k in ("workflows", "server_scripts", "custom_fields",
							  "notifications", "client_scripts")
				)

		# ── (3) Combine + prepend ─────────────────────────────────────
		parts: list[str] = []

		if fkb_rendered:
			parts.append(
				"=== FRAPPE KB CONTEXT (auto-injected, reference only) ===\n"
				"The following platform rules / APIs / idioms were retrieved from\n"
				"the Frappe Knowledge Base based on your request. Follow them\n"
				"alongside the user request; they are NOT part of the request.\n\n"
				+ "\n---\n".join(fkb_rendered)
				+ "\n=========================================================="
			)

		if site_rendered:
			parts.extend(site_rendered)

		if not parts:
			# Nothing to inject from either layer. Record the no-op state so
			# the tracer can distinguish "injected nothing" from "phase didn't
			# run".
			if span:
				span.set(
					injected_kb=[],
					injected_count=0,
					injected_site_doctypes=[],
					injected_site_count=0,
				)
			return

		banner = (
			"\n\n".join(parts)
			+ "\n\n--- USER REQUEST (interpret this verbatim) ---\n"
		)
		ctx.enhanced_prompt = banner + source_prompt

		if span:
			span.set(
				injected_kb=list(ctx.injected_kb),
				injected_count=len(ctx.injected_kb),
				injected_site_doctypes=list(ctx.injected_site_state.keys()),
				injected_site_count=site_artefact_count,
				banner_chars=len(banner),
			)
		logger.info(
			"inject_kb: FKB=%s site=%s for conversation=%s",
			ctx.injected_kb,
			list(ctx.injected_site_state.keys()),
			ctx.conversation_id,
		)

	async def _phase_resolve_mode(self) -> None:
		"""Pick full vs lite. Admin-portal override beats site config which
		beats the 'full' default. Cost tier, NOT the dev/plan/insights/chat mode."""
		if self.ctx.mode != "dev":
			return

		ctx = self.ctx
		if ctx.plan_pipeline_mode:
			ctx.pipeline_mode = ctx.plan_pipeline_mode
			ctx.pipeline_mode_source = "plan"
		else:
			mode = (ctx.conn.site_config.get("pipeline_mode") or "full").lower()
			if mode not in ("full", "lite"):
				mode = "full"
			ctx.pipeline_mode = mode
			ctx.pipeline_mode_source = "site_config"

		logger.info(
			"Pipeline mode resolved for %s: %s (source=%s)",
			ctx.conn.site_id, ctx.pipeline_mode, ctx.pipeline_mode_source,
		)

		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "agent_status",
			"data": {
				"agent": "Orchestrator", "status": "started",
				"phase": "requirement",
				"pipeline_mode": ctx.pipeline_mode,
				"pipeline_mode_source": ctx.pipeline_mode_source,
			},
		})

