"""Module detection — per-module Builder classification for dev mode
(TD-H2 split from ``alfred/orchestrator.py``).

Runs after ``classify_intent`` for dev-mode prompts to pick a module
specialist. Heuristic first (``ModuleRegistry.detect``), LLM fallback
only when heuristic returns None.

V2 (``detect_module``) returns primary only. V3 (``detect_modules``)
returns primary + secondaries for cross-domain prompts; see
``docs/specs/2026-04-22-multi-module-classification.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from alfred.registry.module_loader import ModuleRegistry as _ModuleRegistry

logger = logging.getLogger("alfred.orchestrator.module")


@dataclass
class ModuleDecision:
	"""Result of per-module Builder classification (dev mode only).

	Mirrors IntentDecision. ``module`` is a registered module key or None
	(None means "no module specialist should be invoked" - identical to
	the flag-off path). ``source`` is one of: "heuristic", "classifier",
	"fallback".
	"""

	module: str | None
	reason: str
	confidence: str  # "high" | "medium" | "low"
	source: str

	def to_dict(self) -> dict:
		return {
			"module": self.module,
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


async def _classify_module_llm(prompt: str, site_config: dict) -> str:
	"""Small LLM call that returns a registered module key or "unknown".

	Kept module-level so tests can patch it without standing up the rest
	of the orchestrator.
	"""
	from alfred.llm_client import ollama_chat

	modules = _ModuleRegistry.load().modules()
	if not modules:
		return "unknown"

	system = (
		"You classify the user's Frappe customization request into ONE ERPNext module. "
		f"Valid modules: {', '.join(modules)}, unknown. "
		"Reply with ONLY the module key, no prose, no punctuation."
	)
	reply = await ollama_chat(
		messages=[
			{"role": "system", "content": system},
			{"role": "user", "content": prompt},
		],
		site_config=site_config,
		tier=site_config.get("llm_tier", "triage"),
		max_tokens=16,
		temperature=0.0,
	)
	tag = (reply or "").strip().lower()
	return tag if tag in (*modules, "unknown") else "unknown"


async def detect_module(
	*,
	prompt: str,
	target_doctype: str | None,
	site_config: dict,
) -> ModuleDecision:
	registry = _ModuleRegistry.load()
	module_key, confidence = registry.detect(prompt=prompt, target_doctype=target_doctype)
	if module_key is not None:
		return ModuleDecision(
			module=module_key,
			reason=f"matched heuristic ({confidence}) for {module_key}",
			# ``detect`` only returns confidence=None when module_key is
			# also None (see module_loader.detect), so the cast is safe
			# in this branch.
			confidence=confidence or "",
			source="heuristic",
		)

	try:
		# Lazy re-import so tests that patch
		# ``alfred.orchestrator._classify_module_llm`` affect this call
		# site. Without the indirection, detect_module would resolve the
		# local-module attribute and bypass the package-level patch
		# that existed before the TD-H2 split.
		from alfred.orchestrator import _classify_module_llm as _llm
		tag = await _llm(prompt, site_config)
		if tag == "unknown":
			return ModuleDecision(
				module=None,
				reason="LLM classifier returned unknown",
				confidence="low",
				source="classifier",
			)
		return ModuleDecision(
			module=tag,
			reason=f"LLM classifier returned {tag}",
			confidence="medium",
			source="classifier",
		)
	except Exception as e:  # noqa: BLE001 — LLM-boundary contract; pipeline tests inject arbitrary exceptions into ollama_chat to verify any backend failure falls back to no-module rather than crashing classify_module
		logger.warning("Module classifier failed: %s", e)
		return ModuleDecision(
			module=None,
			reason=f"classifier error: {e}",
			confidence="low",
			source="fallback",
		)


# ── V3 multi-module classification ──────────────────────────────
# Adds primary + secondary modules for prompts that span domains.
# Heuristic path uses ModuleRegistry.detect_all. LLM fallback is
# primary-only - secondaries only come from the heuristic to avoid
# token budget blowup on a second LLM round-trip.
# Spec: docs/specs/2026-04-22-multi-module-classification.md.


@dataclass
class ModulesDecision:
	"""V3 multi-module classification result.

	Mirrors ModuleDecision but carries ``secondary_modules``. When
	``secondary_modules`` is empty, behaviour is V2-equivalent.
	"""

	module: str | None
	secondary_modules: list[str]
	reason: str
	confidence: str
	source: str

	def to_dict(self) -> dict:
		return {
			"module": self.module,
			"secondary_modules": list(self.secondary_modules),
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


async def detect_modules(
	*,
	prompt: str,
	target_doctype: str | None,
	site_config: dict,
) -> ModulesDecision:
	"""Heuristic + LLM fallback for primary + secondaries.

	Heuristic uses ModuleRegistry.detect_all. LLM fallback returns a
	primary-only decision (secondaries stay empty) to keep cost bounded.
	"""
	registry = _ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt=prompt, target_doctype=target_doctype,
	)
	if primary is not None:
		return ModulesDecision(
			module=primary,
			secondary_modules=secondaries,
			reason=f"matched heuristic ({confidence}) for {primary}; secondaries={secondaries}",
			confidence=confidence,
			source="heuristic",
		)

	try:
		# Lazy re-import through the package — see detect_module for
		# the rationale.
		from alfred.orchestrator import _classify_module_llm as _llm
		tag = await _llm(prompt, site_config)
		if tag == "unknown":
			return ModulesDecision(
				module=None, secondary_modules=[],
				reason="LLM classifier returned unknown",
				confidence="low", source="classifier",
			)
		return ModulesDecision(
			module=tag, secondary_modules=[],
			reason=f"LLM classifier returned {tag}",
			confidence="medium", source="classifier",
		)
	except Exception as e:  # noqa: BLE001 — LLM-boundary contract; same as classify_module — any backend failure degrades to no-module rather than crash primary + secondary detection
		logger.warning("Multi-module classifier failed: %s", e)
		return ModulesDecision(
			module=None, secondary_modules=[],
			reason=f"classifier error: {e}",
			confidence="low", source="fallback",
		)
