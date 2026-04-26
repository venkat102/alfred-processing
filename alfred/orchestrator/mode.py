"""Mode classification — decides which of dev / plan / insights / chat
handles a given prompt (TD-H2 split from ``alfred/orchestrator.py``).

Design notes:
  - Pre-classification fast path handles obvious cases (greetings, build
    verbs, common read-only query phrasings) without an LLM call. Cheap
    and deterministic.
  - LLM classification uses the same ollama_chat client as
    ``enhance_prompt`` - one call, low max_tokens, temp 0, JSON output.
  - Confidence-based fallback: on low confidence or parse failure, pick
    the SAFEST mode. That's "dev" if there's an active plan in memory
    (user is continuing planned work) else "chat" (conversational is
    cheap to re-route; crew runs are expensive and noisy).
  - Plan mode is the only output the fast-path never produces - plan
    classification is harder to do with string matching and benefits
    from an LLM call reading the whole sentence for design-question cues.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.state.conversation_memory import ConversationMemory

logger = logging.getLogger("alfred.orchestrator.mode")

_VALID_MODES = ("dev", "plan", "insights", "chat")
_VALID_OVERRIDES = ("auto", "dev", "plan", "insights")

# Cap on conversation memory text sent to the classifier. The classifier
# runs with num_ctx=2048; the system prompt already consumes ~350 tokens
# and we need room for the user prompt + generated JSON, so leave memory
# at ~1000 chars (~250 tokens). Longer memory is truncated with a marker
# so the LLM knows the context was clipped.
_MEMORY_CONTEXT_CHAR_CAP = 1000


def is_enabled() -> bool:
	"""Feature-flag check for the mode orchestrator.

	Default is False (see alfred.config.Settings). Pydantic coerces
	"1"/"true"/"yes"/"on" to True; anything else — including garbage
	strings like "maybe" or empty "" that would otherwise raise
	ValidationError at Settings construction — is treated as disabled.
	"""
	from pydantic import ValidationError

	from alfred.config import get_settings
	try:
		return get_settings().ALFRED_ORCHESTRATOR_ENABLED
	except ValidationError:
		# Malformed flag value in the env should never crash the
		# pipeline; default to the safe off state.
		return False


# Exact-match greetings and short conversational turns. Hit here means no
# LLM call. Keep this set small and obvious - anything borderline should
# go through the LLM classifier so it can reason about context.
_FAST_PATH_CHAT_EXACT = {
	"hi", "hello", "hey", "yo", "hiya", "howdy", "greetings",
	"thanks", "thank you", "thx", "ty",
	"ok", "okay", "cool", "sure",
	"bye", "goodbye", "cya", "later",
	"good morning", "good afternoon", "good evening",
	"what can you do", "what can you do?",
	"help", "help me",
}

# Strong dev signals - imperative verbs that unambiguously ask for a build.
# If these appear at the start of the prompt, fast-path to dev regardless
# of classifier confidence.
_FAST_PATH_DEV_PREFIXES = (
	"add a ", "add the ", "add an ",
	"create a ", "create an ", "create the ",
	"build a ", "build an ", "build the ", "build me ",
	"make a ", "make an ", "make the ",
)

# Strong insights signals - interrogatives that ask about current site state.
# These are phrasings where the user clearly wants information about what's
# already on their site. Anything ambiguous (e.g. "show me how to...") falls
# through to the LLM classifier so it can read the whole sentence.
_FAST_PATH_INSIGHTS_PREFIXES = (
	"what doctypes ",
	"which doctypes ",
	"what workflows ",
	"which workflows ",
	"what notifications ",
	"which notifications ",
	"what custom fields ",
	"which custom fields ",
	"what server scripts ",
	"which server scripts ",
	"what client scripts ",
	"which client scripts ",
	"what customizations ",
	"which customizations ",
	"what modules ",
	"which modules ",
	"list my ",
	"list all ",
	"show me my ",
	"show me the ",
	"show me all ",
	"do i have ",
	"does my site have ",
	"how many ",
)

_FAST_PATH_INSIGHTS_PATTERNS = (
	# "what * do i have" / "what * does the site have"
	" do i have",
	" does the site have",
	" does my site have",
	" are on my site",
	" are active on",
	" are installed on",
)

# Analytics / "top N" verbs that should route to Insights rather than Dev.
# Covers "show top", "list the top", counting, summaries, report-on phrasings.
# Deploy verbs (build/create/add/make) are checked first in _fast_path() so
# "build a Report DocType for top customers" still routes to Dev.
_FAST_PATH_INSIGHTS_ANALYTICS_PREFIXES = (
	"show top ",
	"show the top ",
	"show me top ",
	"list the top ",
	"count of ",
	"summarize ",
	"summarise ",
	"summary of ",
	"report on ",
	"report me ",
)

_CLASSIFIER_SYSTEM_PROMPT = """\
You classify user prompts into one of four modes for a Frappe customization assistant.

Modes:
- dev: user wants to BUILD, CREATE, MODIFY, or DEPLOY something.
       Examples: "add a priority field", "create a DocType", "build the workflow",
                 "approve and deploy", "now delete that field".
- plan: user wants to DISCUSS AN APPROACH before building.
        Examples: "how would we approach adding approval?", "what's the best way to...",
                  "design a solution for...", "before we build, let's plan".
- insights: user wants INFORMATION about their current Frappe site.
            Examples: "what DocTypes do I have?", "show me my notifications",
                      "explain this server script", "which workflows are active?".
- chat: greetings, thanks, meta questions about Alfred itself, or anything
        conversational that doesn't fit the above.
        Examples: "hi", "thanks", "what can you do?", "how does Alfred work?".

If the user refers to "it" / "that" / "the plan", resolve the referent from
the conversation context block if one is present. Short follow-ups like
"build it" / "do it" / "go ahead" after a plan-mode discussion should be
classified as dev.

Return ONLY valid JSON, nothing else:
{"mode": "dev|plan|insights|chat", "reason": "one-sentence justification", "confidence": "high|medium|low"}
"""


@dataclass
class ModeDecision:
	"""Result of orchestrator classification.

	`source` tells callers how the decision was made (for tracing + UI):
	  - "override": user manually forced this mode, LLM skipped
	  - "fast_path": matched a static rule, LLM skipped
	  - "classifier": LLM returned this mode
	  - "fallback": classifier failed or returned low confidence, picked safe default
	"""

	mode: str
	reason: str
	confidence: str  # "high" | "medium" | "low"
	source: str

	def to_dict(self) -> dict:
		return {
			"mode": self.mode,
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


def _normalize_override(override: str | None) -> str:
	"""Lowercase + validate a manual override value."""
	if not override:
		return "auto"
	val = str(override).strip().lower()
	return val if val in _VALID_OVERRIDES else "auto"


def _normalize_mode(mode: str | None) -> str | None:
	"""Lowercase + validate a mode string from classifier output."""
	if not mode:
		return None
	val = str(mode).strip().lower()
	return val if val in _VALID_MODES else None


def _has_active_plan(memory: ConversationMemory | None) -> bool:
	"""Check if the conversation has an active plan the user could reference.

	Phase C adds a proper `active_plan` slot on ConversationMemory. Until
	then, this returns False and the Phase A classifier has no plan context.
	"""
	if memory is None:
		return False
	return bool(getattr(memory, "active_plan", None))


def _fast_path(prompt: str) -> str | None:
	"""Deterministic pre-classification. Returns a mode or None (= use LLM).

	Handles: empty prompt, exact greetings, imperative build prefixes, and
	common read-only query phrasings for Insights mode.
	"""
	if not prompt or not prompt.strip():
		return "chat"

	normalized = prompt.strip().lower().rstrip("!.?,")

	if normalized in _FAST_PATH_CHAT_EXACT:
		return "chat"

	for prefix in _FAST_PATH_DEV_PREFIXES:
		if normalized.startswith(prefix):
			return "dev"

	# Insights: interrogative prefixes that unambiguously ask for info
	# about the user's current site state.
	for prefix in _FAST_PATH_INSIGHTS_PREFIXES:
		if normalized.startswith(prefix):
			return "insights"

	# Insights: analytics / "top N" / summary phrasings.
	for prefix in _FAST_PATH_INSIGHTS_ANALYTICS_PREFIXES:
		if normalized.startswith(prefix):
			return "insights"

	# Insights: substring patterns like "do I have ..." / "are active on ..."
	# that work regardless of leading adjective/adverb.
	for pattern in _FAST_PATH_INSIGHTS_PATTERNS:
		if pattern in normalized:
			return "insights"

	return None


_JSON_OBJECT_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_classifier_output(text: str) -> tuple[str | None, str, str]:
	"""Extract mode/reason/confidence from an LLM response.

	The model is prompted for strict JSON but local models sometimes wrap in
	code fences or add prose before/after. We strip fences, try direct parse,
	fall back to the first balanced object-like substring.

	Returns (mode_or_None, reason, confidence). mode=None means parse failed.
	"""
	if not text:
		return None, "", "low"

	cleaned = text.strip()
	# Strip surrounding code fences if any
	if cleaned.startswith("```"):
		lines = cleaned.splitlines()
		if lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		cleaned = "\n".join(lines).strip()

	try:
		parsed = json.loads(cleaned)
	except json.JSONDecodeError:
		# Local model wrapped JSON in prose; try the first balanced
		# {...} block in the cleaned text.
		match = _JSON_OBJECT_RE.search(cleaned)
		if not match:
			# Log once so prompt regressions are visible — the fallback
			# path silently picked "low" confidence for weeks before
			# (master c124f9b).
			logger.warning(
				"mode classifier JSON parse failed, no object match in output: %r",
				cleaned[:160],
			)
			return None, "", "low"
		try:
			parsed = json.loads(match.group(0))
		except json.JSONDecodeError:
			logger.warning(
				"mode classifier JSON parse failed on regex-extracted object: %r",
				match.group(0)[:160],
			)
			return None, "", "low"

	if not isinstance(parsed, dict):
		return None, "", "low"

	mode = _normalize_mode(parsed.get("mode"))
	reason = str(parsed.get("reason") or "").strip()
	conf_raw = str(parsed.get("confidence") or "medium").strip().lower()
	confidence = conf_raw if conf_raw in ("high", "medium", "low") else "medium"
	return mode, reason, confidence


def _clip_memory_context(text: str, cap: int = _MEMORY_CONTEXT_CHAR_CAP) -> str:
	"""Truncate memory context to keep it within the classifier num_ctx budget.

	Keeps the tail of the context (most recent turns) and prepends a clipped
	marker so the classifier knows the context is partial.
	"""
	if len(text) <= cap:
		return text
	return "[... older context clipped ...]\n" + text[-cap:]


async def _classify_with_llm(
	prompt: str,
	memory_context: str,
	site_config: dict,
) -> tuple[str | None, str, str]:
	"""Call the LLM to classify the prompt. Returns (mode, reason, confidence)
	or (None, "", "low") on any failure."""
	from alfred.llm_client import ollama_chat

	user_parts = []
	if memory_context:
		user_parts.append(_clip_memory_context(memory_context))
		user_parts.append("")
	user_parts.append(f"Prompt: {prompt}")

	timeout = int(site_config.get("classifier_timeout", 60))

	try:
		raw = await ollama_chat(
			messages=[
				{"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
				{"role": "user", "content": "\n".join(user_parts)},
			],
			site_config=site_config,
			tier="triage",
			max_tokens=128,
			temperature=0.0,
			num_ctx_override=2048,  # Classifier prompt is small
			timeout=timeout,
		)
		logger.debug("Orchestrator classifier raw output: %r", raw[:300])
		return _parse_classifier_output(raw)
	except Exception as e:  # noqa: BLE001 — LLM-boundary contract; tests (test_llm_fallbacks etc) mock ollama_chat with RuntimeError to verify any backend failure falls back to low-confidence rather than crashing the orchestrator
		logger.warning("Orchestrator classifier call failed: %s: %s", type(e).__name__, e)
		return None, "", "low"


async def classify_mode(
	prompt: str,
	memory: ConversationMemory | None,
	manual_override: str | None,
	site_config: dict,
	force_dev_override: bool = False,
) -> ModeDecision:
	"""Decide which mode should handle this prompt.

	Priority order:
	  1. Analytics-shape redirect: if manual override is "dev" but the prompt
	     is a read-side analytics / Q&A phrasing, redirect to insights (unless
	     ``force_dev_override`` is set — the user clicked "Run in Dev anyway").
	  2. Manual override (if != "auto") - LLM skipped
	  3. Fast-path match (greetings, imperative build prefixes) - LLM skipped
	  4. LLM classifier call
	  5. Confidence-based fallback if classifier fails or returns low confidence

	Never raises - always returns a valid ModeDecision. On complete failure
	returns a safe-default chat decision. Every decision increments the
	Prometheus `alfred_orchestrator_decisions_total` counter so operators
	can see whether the classifier LLM is actually running in production
	vs always falling back.
	"""
	# Imported lazily to avoid a circular import at module load time
	# (orchestrator.intent re-uses the analytics-query detector).
	from alfred.obs.metrics import orchestrator_decisions_total
	from alfred.orchestrator.intent import _looks_like_analytics_query

	def _record(decision: ModeDecision) -> ModeDecision:
		try:
			orchestrator_decisions_total.labels(
				source=decision.source, mode=decision.mode,
			).inc()
		except Exception:  # noqa: BLE001 — metrics best-effort; must not block the mode decision from reaching the caller
			pass
		return decision

	override = _normalize_override(manual_override)

	# Hybrid redirect: "Dev" override + analytics-shape prompt → route to
	# insights and surface a banner source so the UI can offer a one-click
	# "Run in Dev anyway" that re-sends with force_dev_override=True.
	# Without this, a user with the Dev toggle flipped on asks an analytics
	# question and the generic Developer hallucinates a changeset.
	if (
		override == "dev"
		and not force_dev_override
		and _looks_like_analytics_query(prompt)
	):
		return _record(ModeDecision(
			mode="insights",
			reason=(
				"Prompt looks like an analytics / Q&A request; routed to "
				"Insights even though Dev was selected. Click 'Run in Dev "
				"anyway' to override."
			),
			confidence="high",
			source="analytics_redirect",
		))

	if override != "auto":
		return _record(ModeDecision(
			mode=override,
			reason=f"User forced mode={override} via manual override",
			confidence="high",
			source="override",
		))

	fast = _fast_path(prompt)
	if fast is not None:
		return _record(ModeDecision(
			mode=fast,
			reason=f"Fast-path match ({fast})",
			confidence="high",
			source="fast_path",
		))

	memory_context = ""
	if memory is not None:
		try:
			memory_context = memory.render_for_prompt()
		except Exception as e:  # noqa: BLE001 — memory is a duck-typed input (test_memory_render_failure_does_not_crash uses a custom class that raises RuntimeError); render must never crash classify_mode regardless of which subclass the caller provided
			logger.warning("memory.render_for_prompt failed: %s", e)
			memory_context = ""

	try:
		# Lazy re-import so tests that patch
		# ``alfred.orchestrator._classify_with_llm`` affect this call
		# site. Without the indirection, classify_mode would resolve
		# the local-module attribute and bypass the package-level
		# patch that existed before the TD-H2 split.
		from alfred.orchestrator import _classify_with_llm as _llm
		mode, reason, confidence = await _llm(
			prompt, memory_context, site_config or {}
		)
	except Exception as e:  # noqa: BLE001 — defensive; _classify_with_llm catches its own LLM/network exceptions and never raises in normal use, but a logic bug here must not crash the whole mode decision
		logger.warning("Orchestrator classifier wrapper raised: %s", e)
		mode, reason, confidence = None, "", "low"

	if mode is not None and confidence != "low":
		return _record(ModeDecision(
			mode=mode,
			reason=reason or "LLM classifier decision",
			confidence=confidence,
			source="classifier",
		))

	# Fallback. Pick the safest default.
	fallback_mode = "dev" if _has_active_plan(memory) else "chat"
	fallback_reason = (
		f"Classifier {'low-confidence' if mode else 'unavailable'}; "
		f"defaulted to {fallback_mode}"
	)
	return _record(ModeDecision(
		mode=fallback_mode,
		reason=fallback_reason,
		confidence="low",
		source="fallback",
	))


# ── Intent classification (Dev mode) ─────────────────────────────
# Runs only for dev-mode prompts to pick a per-intent Builder
# specialist. Mirrors classify_mode(): heuristic first, LLM fallback,
# "unknown" on failure. Spec:
# docs/specs/2026-04-21-doctype-builder-specialist.md

_SUPPORTED_INTENTS: tuple[str, ...] = ("create_doctype", "create_report")

_HEURISTIC_INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
	"create_doctype": (
		"create a doctype",
		"create doctype",
		"new doctype",
		"add a doctype",
		"add doctype",
		"build a doctype",
		"make a doctype",
	),
	"create_report": (
		"save as report",
		"save this as a report",
		"create a report",
		"make a report",
		"build a report",
		"new report",
	),
}


@dataclass
class IntentDecision:
	"""Result of per-intent Builder classification (dev mode only).

	Mirrors ``ModeDecision`` in shape. ``intent`` is one of the keys in
	``_SUPPORTED_INTENTS`` or the literal ``"unknown"``. ``source`` is
	one of: ``"heuristic"``, ``"classifier"``, ``"fallback"``.
	"""

	intent: str
	reason: str
	confidence: str  # "high" | "medium" | "low"
	source: str

	def to_dict(self) -> dict:
		return {
			"intent": self.intent,
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


def _match_intent_heuristic(prompt: str) -> str | None:
	low = prompt.lower()
	for intent, patterns in _HEURISTIC_INTENT_PATTERNS.items():
		if any(p in low for p in patterns):
			return intent
	return None


async def _classify_intent_llm(prompt: str, site_config: dict) -> str:
	"""Small LLM call that returns a supported intent key or ``"unknown"``.

	Kept as a module-level function so tests can patch it without
	standing up the rest of the orchestrator.
	"""
	from alfred.llm_client import ollama_chat

	system = (
		"You classify the user's Frappe customization request into ONE intent. "
		f"Valid intents: {', '.join(_SUPPORTED_INTENTS)}, unknown. "
		"Reply with ONLY the intent key, no prose, no punctuation."
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
	return tag if tag in (*_SUPPORTED_INTENTS, "unknown") else "unknown"


async def classify_intent(prompt: str, site_config: dict) -> IntentDecision:
	heur = _match_intent_heuristic(prompt)
	if heur is not None:
		return IntentDecision(
			intent=heur,
			reason=f"matched heuristic pattern for {heur}",
			confidence="high",
			source="heuristic",
		)

	try:
		tag = await _classify_intent_llm(prompt, site_config)
		return IntentDecision(
			intent=tag,
			reason=f"LLM classifier returned {tag}",
			confidence="medium" if tag != "unknown" else "low",
			source="classifier",
		)
	except Exception as e:  # noqa: BLE001 — classifier boundary; LLM/network/parse failures must degrade to intent=unknown so the pipeline still runs
		logger.warning("Intent classifier failed: %s", e)
		return IntentDecision(
			intent="unknown",
			reason=f"classifier error: {e}",
			confidence="low",
			source="fallback",
		)


# ── Module detection (Dev mode) ─────────────────────────────────
# Runs after classify_intent for dev-mode prompts to pick a module
# specialist. Heuristic first (ModuleRegistry.detect), LLM fallback
# only when heuristic returns None. Spec:
# docs/specs/2026-04-22-module-specialists.md

from alfred.registry.module_loader import ModuleRegistry as _ModuleRegistry


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
		assert confidence is not None  # detect() pairs them
		return ModuleDecision(
			module=module_key,
			reason=f"matched heuristic ({confidence}) for {module_key}",
			confidence=confidence,
			source="heuristic",
		)

	try:
		tag = await _classify_module_llm(prompt, site_config)
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
	except Exception as e:  # noqa: BLE001 — classifier boundary; LLM/network/parse failures must degrade to module=None so the pipeline still runs
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
		tag = await _classify_module_llm(prompt, site_config)
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
	except Exception as e:  # noqa: BLE001 — classifier boundary; LLM/network/parse failures must degrade to module=None so the pipeline still runs
		logger.warning("Multi-module classifier failed: %s", e)
		return ModulesDecision(
			module=None, secondary_modules=[],
			reason=f"classifier error: {e}",
			confidence="low", source="fallback",
		)
