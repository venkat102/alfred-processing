"""Intent classification — dev-mode-only per-intent Builder dispatcher
(TD-H2 split from ``alfred/orchestrator.py``).

Runs only for dev-mode prompts to pick a per-intent Builder specialist.
Heuristic first (keyword tables), LLM fallback, "unknown" on failure.

Spec: ``docs/specs/2026-04-21-doctype-builder-specialist.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("alfred.orchestrator.intent")


_SUPPORTED_INTENTS: tuple[str, ...] = (
	"create_doctype",
	"create_custom_field",
	"create_role_with_permissions",
	"create_property_setter",
	"create_user_permission",
	"create_report",
	"create_dashboard",
	"create_dashboard_chart",
	"create_number_card",
	"create_auto_email_report",
	"create_server_script",
	"create_client_script",
	"create_notification",
	"create_workflow",
	"create_webhook",
	"create_auto_repeat",
	"create_assignment_rule",
	"create_print_format",
	"create_letter_head",
	"create_email_template",
	"create_web_form",
	"update_print_settings",
)

# Heuristic substring matches (lowercased prompt). Order matters: more
# specific patterns MUST live before more general ones, because dict
# iteration preserves insertion order (Python 3.7+) and the first
# matching family wins. Specifically:
#   - Schema-family role/custom-field patterns run BEFORE create_doctype
#     so "add a role on X DocType" doesn't match create_doctype first.
#   - Reports-family number_card / dashboard_chart / dashboard patterns
#     run BEFORE create_report so "create a dashboard with a chart"
#     doesn't match create_report first.
_HEURISTIC_INTENT_PATTERNS: dict[str, tuple[str, ...]] = {
	"create_role_with_permissions": (
		"create a role",
		"create role",
		"new role",
		"add a role",
		"add role",
		"role with permission",
		"role with permissions",
		"grant permission",
		"grant permissions",
		"give permission",
		"give permissions",
	),
	"create_property_setter": (
		"make it required",
		"make it mandatory",
		"mark as required",
		"mark as mandatory",
		"make required on",
		"change the label",
		"rename the field",
		"hide the field",
		"show the field",
		"make read only on",
		"mark as read only on",
		"change the default of",
		"set the title field",
		"set title_field",
		"override the",
		"property setter",
	),
	"create_user_permission": (
		"restrict user",
		"restrict the user",
		"user can only see",
		"user should only see",
		"limit user to",
		"grant user access to",
		"give user access to",
		"only allow user",
		"user permission",
	),
	"create_custom_field": (
		"add a custom field",
		"add custom field",
		"new custom field",
		"create a custom field",
		"create custom field",
		"add a field to",
		"add a field on",
	),
	"create_doctype": (
		"create a doctype",
		"create doctype",
		"new doctype",
		"add a doctype",
		"add doctype",
		"build a doctype",
		"make a doctype",
	),
	"create_number_card": (
		"number card",
		"kpi card",
		"metric card",
		"count card",
	),
	"create_dashboard_chart": (
		"dashboard chart",
		"add a chart",
		"add chart",
		"create a chart",
		"new chart",
		"build a chart",
	),
	"create_dashboard": (
		"create a dashboard",
		"create dashboard",
		"new dashboard",
		"add a dashboard",
		"add dashboard",
		"build a dashboard",
	),
	"create_auto_email_report": (
		"auto email report",
		"email the report",
		"email this report",
		"schedule the report",
		"schedule this report",
		"send the report every",
		"send report every",
		"recurring report",
		"weekly report email",
		"daily report email",
		"monthly report email",
	),
	"create_report": (
		"save as report",
		"save this as a report",
		"create a report",
		"make a report",
		"build a report",
		"new report",
	),
	"create_workflow": (
		"create a workflow",
		"create workflow",
		"new workflow",
		"add a workflow",
		"add workflow",
		"build a workflow",
		"approval workflow",
		"approval flow",
		"review workflow",
	),
	"create_webhook": (
		"webhook",
		"post to url",
		"post to a url",
		"send data to url",
		"send data to external",
		"ping an external",
		"ping external",
		"http callback",
		"outbound http",
		"call external api when",
	),
	"create_auto_repeat": (
		"auto repeat",
		"auto-repeat",
		"recurring invoice",
		"recurring document",
		"recurring sales order",
		"repeat monthly",
		"repeat weekly",
		"repeat daily",
		"every month create",
		"every week create",
		"subscription",
	),
	"create_assignment_rule": (
		"assignment rule",
		"auto assign",
		"auto-assign",
		"round robin",
		"round-robin",
		"load balancing",
		"load-balancing",
		"distribute tickets",
		"distribute leads",
		"route to",
		"routing rule",
	),
	"create_notification": (
		"create a notification",
		"create notification",
		"new notification",
		"add a notification",
		"add notification",
		"send an email when",
		"send email when",
		"email the ",
		"notify the ",
		"alert the ",
	),
	"create_server_script": (
		"server script",
		"before save",
		"after save",
		"before submit",
		"on submit",
		"validate the ",
		"block save",
		"block submit",
		"throw an error",
		"throw if",
	),
	"create_client_script": (
		"client script",
		"on form load",
		"on field change",
		"custom button on",
		"hide field",
		"show field",
	),
	"create_print_format": (
		"print format",
		"invoice template",
		"invoice layout",
		"quote template",
		"quote layout",
		"receipt template",
		"receipt layout",
		"document layout",
	),
	"create_letter_head": (
		"letter head",
		"letterhead",
		"company header",
		"company footer",
		"branded header",
		"branded footer",
	),
	"create_email_template": (
		"email template",
	),
	"create_web_form": (
		"web form",
		"public form",
		"portal form",
		"external form",
		"form on the website",
		"form on the portal",
	),
	"update_print_settings": (
		"print settings",
		"site print config",
		"site-wide print",
		"change pdf generator",
		"switch pdf generator",
		"enable print for draft",
		"allow print for draft",
		"allow print for cancelled",
		"allow print cancelled",
		"enable raw printing",
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


def _looks_like_analytics_query(prompt: str) -> bool:
	"""Return True if the prompt is a read-side analytics / Q&A phrasing
	that should never be interpreted as a build intent.

	Mirrors the mode-level Insights fast-path
	(``_FAST_PATH_INSIGHTS_*`` in ``alfred.orchestrator.mode``).
	Dev-side guardrail: even if ``classify_mode`` somehow lands on dev
	(manual override, active plan, classifier miss), a prompt like
	"show top 10 customers by revenue" must not get routed to a Builder
	specialist - the LLM intent classifier would pick a random intent
	from 22 options and hallucinate a changeset out of thin air.
	"""
	# Imported lazily to avoid a circular import: mode.py imports
	# _looks_like_analytics_query from this module, and importing the
	# tables at module load time would create a cycle.
	from alfred.orchestrator.mode import (
		_FAST_PATH_INSIGHTS_ANALYTICS_PREFIXES,
		_FAST_PATH_INSIGHTS_PATTERNS,
		_FAST_PATH_INSIGHTS_PREFIXES,
	)

	if not prompt:
		return False
	normalized = prompt.strip().lower().rstrip("!.?,")
	if not normalized:
		return False
	for prefix in _FAST_PATH_INSIGHTS_ANALYTICS_PREFIXES:
		if normalized.startswith(prefix):
			return True
	for prefix in _FAST_PATH_INSIGHTS_PREFIXES:
		if normalized.startswith(prefix):
			return True
	for pattern in _FAST_PATH_INSIGHTS_PATTERNS:
		if pattern in normalized:
			return True
	return False


async def _classify_intent_llm(prompt: str, site_config: dict) -> str:
	"""Small LLM call that returns a supported intent key or ``"unknown"``.

	Kept as a module-level function so tests can patch it without
	standing up the rest of the orchestrator.
	"""
	from alfred.llm_client import ollama_chat

	system = (
		"You classify the user's Frappe customization BUILD request into ONE intent. "
		f"Valid intents: {', '.join(_SUPPORTED_INTENTS)}, unknown.\n"
		"\n"
		"Rules:\n"
		"- Return an intent ONLY when the prompt unambiguously names BOTH a "
		"build verb (create / add / make / build / deploy / set up / configure / "
		"enable / disable) AND a target primitive matching one of the intents.\n"
		"- If the prompt is a QUESTION about the current site state "
		"(\"what ...\", \"which ...\", \"show me ...\", \"list ...\", "
		"\"how many ...\", \"top N ...\"), return unknown - that is a read-only "
		"analytics / Insights request, NOT a build request.\n"
		"- If the prompt is a GREETING, small talk, or ambiguous, return unknown.\n"
		"- When in doubt between two build intents, prefer unknown - a wrong "
		"intent hallucinates a full changeset; unknown falls back to the "
		"generic Developer which can ask the user to clarify.\n"
		"\n"
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
	if _looks_like_analytics_query(prompt):
		return IntentDecision(
			intent="unknown",
			reason="prompt is a read-side analytics / Q&A phrasing (dev-side Insights guardrail)",
			confidence="high",
			source="analytics_guardrail",
		)

	heur = _match_intent_heuristic(prompt)
	if heur is not None:
		return IntentDecision(
			intent=heur,
			reason=f"matched heuristic pattern for {heur}",
			confidence="high",
			source="heuristic",
		)

	try:
		# Lazy re-import so tests that patch
		# ``alfred.orchestrator._classify_intent_llm`` affect this call
		# site. Without the indirection, classify_intent would resolve
		# the local-module attribute and bypass the package-level
		# patch that existed before the TD-H2 split.
		from alfred.orchestrator import _classify_intent_llm as _llm
		tag = await _llm(prompt, site_config)
		return IntentDecision(
			intent=tag,
			reason=f"LLM classifier returned {tag}",
			confidence="medium" if tag != "unknown" else "low",
			source="classifier",
		)
	except Exception as e:  # noqa: BLE001 — LLM-boundary contract; tests (test_classify_intent.test_classifier_failure_falls_back_to_unknown) inject RuntimeError to verify any backend failure degrades to fallback rather than crashing the dispatcher
		logger.warning("Intent classifier failed: %s", e)
		return IntentDecision(
			intent="unknown",
			reason=f"classifier error: {e}",
			confidence="low",
			source="fallback",
		)
