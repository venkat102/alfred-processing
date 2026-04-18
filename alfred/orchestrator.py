"""Mode orchestrator - decides how a prompt flows through the pipeline.

Alfred supports four modes:
  - dev: run the agent crew and produce a deployable changeset
  - plan: run the 3-agent planning crew and produce a plan doc
  - insights: read-only Q&A about the site state
  - chat: pure conversational reply, no crew, no tools

This module is the decision point. It returns one of those four modes per
prompt, taking into account:
  - the raw prompt text
  - optional conversation memory context (so follow-ups like "build it" can
    be resolved against a proposed plan from an earlier turn)
  - an optional manual override the user set in the chat UI header switcher
  - the site's LLM config (for the classifier LLM call)

Design notes:
  - Pre-classification fast path handles obvious cases (greetings, build
    verbs, common read-only query phrasings) without an LLM call. Cheap
    and deterministic.
  - LLM classification uses the same litellm pattern as `enhance_prompt` -
    one streaming call, low max_tokens, temp 0, JSON-shaped output.
  - Confidence-based fallback: on low confidence or parse failure, pick the
    SAFEST mode. That's "dev" if there's an active plan in memory (user is
    continuing planned work) else "chat" (conversational is cheap to
    re-route; crew runs are expensive and noisy).
  - Plan mode is the only output the fast-path never produces - plan
    classification is harder to do with string matching and benefits from
    an LLM call reading the whole sentence for design-question cues.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.state.conversation_memory import ConversationMemory

logger = logging.getLogger("alfred.orchestrator")

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

	Off by default. Accepts 1/true/yes (case-insensitive) - matches the
	parsing convention used by ALFRED_REFLECTION_ENABLED and
	ALFRED_TRACING_ENABLED.
	"""
	return os.environ.get("ALFRED_ORCHESTRATOR_ENABLED", "").strip().lower() in {
		"1", "true", "yes", "on",
	}

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


def _has_active_plan(memory: "ConversationMemory | None") -> bool:
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
	except Exception:
		match = _JSON_OBJECT_RE.search(cleaned)
		if not match:
			return None, "", "low"
		try:
			parsed = json.loads(match.group(0))
		except Exception:
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
	except Exception as e:
		logger.warning("Orchestrator classifier call failed: %s: %s", type(e).__name__, e)
		return None, "", "low"


async def classify_mode(
	prompt: str,
	memory: "ConversationMemory | None",
	manual_override: str | None,
	site_config: dict,
) -> ModeDecision:
	"""Decide which mode should handle this prompt.

	Priority order:
	  1. Manual override (if != "auto") - LLM skipped
	  2. Fast-path match (greetings, imperative build prefixes) - LLM skipped
	  3. LLM classifier call
	  4. Confidence-based fallback if classifier fails or returns low confidence

	Never raises - always returns a valid ModeDecision. On complete failure
	returns a safe-default chat decision.
	"""
	override = _normalize_override(manual_override)
	if override != "auto":
		return ModeDecision(
			mode=override,
			reason=f"User forced mode={override} via manual override",
			confidence="high",
			source="override",
		)

	fast = _fast_path(prompt)
	if fast is not None:
		return ModeDecision(
			mode=fast,
			reason=f"Fast-path match ({fast})",
			confidence="high",
			source="fast_path",
		)

	memory_context = ""
	if memory is not None:
		try:
			memory_context = memory.render_for_prompt()
		except Exception as e:
			logger.warning("memory.render_for_prompt failed: %s", e)
			memory_context = ""

	try:
		mode, reason, confidence = await _classify_with_llm(
			prompt, memory_context, site_config or {}
		)
	except Exception as e:
		logger.warning("Orchestrator classifier wrapper raised: %s", e)
		mode, reason, confidence = None, "", "low"

	if mode is not None and confidence != "low":
		return ModeDecision(
			mode=mode,
			reason=reason or "LLM classifier decision",
			confidence=confidence,
			source="classifier",
		)

	# Fallback. Pick the safest default.
	fallback_mode = "dev" if _has_active_plan(memory) else "chat"
	fallback_reason = (
		f"Classifier {'low-confidence' if mode else 'unavailable'}; "
		f"defaulted to {fallback_mode}"
	)
	return ModeDecision(
		mode=fallback_mode,
		reason=fallback_reason,
		confidence="low",
		source="fallback",
	)
