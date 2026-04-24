"""Mode / intent / module orchestration for the Alfred pipeline.

TD-H2 split: this package replaces the single-file ``alfred.orchestrator``
module. The public surface is unchanged — every name previously reachable
via ``from alfred.orchestrator import X`` is re-exported here, and
``patch("alfred.orchestrator.X", ...)`` in tests continues to work
because the attributes still hang off ``alfred.orchestrator`` (this
package's ``__init__``).

Layout:
  - ``alfred.orchestrator.mode``    — dev/plan/insights/chat mode dispatcher
  - ``alfred.orchestrator.intent``  — per-intent Builder classifier
  - ``alfred.orchestrator.module``  — per-module Builder classifier

Alfred supports four modes:
  - dev: run the agent crew and produce a deployable changeset
  - plan: run the 3-agent planning crew and produce a plan doc
  - insights: read-only Q&A about the site state
  - chat: pure conversational reply, no crew, no tools
"""

from __future__ import annotations

import logging

# Keep the legacy module-level logger alive so callers that reach for
# ``alfred.orchestrator.logger`` (none today, but cheap insurance) still
# resolve it.
logger = logging.getLogger("alfred.orchestrator")

from alfred.orchestrator.mode import (  # noqa: E402 (re-export block)
	_CLASSIFIER_SYSTEM_PROMPT,
	_FAST_PATH_CHAT_EXACT,
	_FAST_PATH_DEV_PREFIXES,
	_FAST_PATH_INSIGHTS_ANALYTICS_PREFIXES,
	_FAST_PATH_INSIGHTS_PATTERNS,
	_FAST_PATH_INSIGHTS_PREFIXES,
	_JSON_OBJECT_RE,
	_MEMORY_CONTEXT_CHAR_CAP,
	_VALID_MODES,
	_VALID_OVERRIDES,
	ModeDecision,
	_classify_with_llm,
	_clip_memory_context,
	_fast_path,
	_has_active_plan,
	_normalize_mode,
	_normalize_override,
	_parse_classifier_output,
	classify_mode,
	is_enabled,
)
from alfred.orchestrator.intent import (  # noqa: E402
	_HEURISTIC_INTENT_PATTERNS,
	_SUPPORTED_INTENTS,
	IntentDecision,
	_classify_intent_llm,
	_looks_like_analytics_query,
	_match_intent_heuristic,
	classify_intent,
)
from alfred.orchestrator.module import (  # noqa: E402
	ModuleDecision,
	ModulesDecision,
	_classify_module_llm,
	_ModuleRegistry,
	detect_module,
	detect_modules,
)

__all__ = [
	"ModeDecision",
	"IntentDecision",
	"ModuleDecision",
	"ModulesDecision",
	"classify_mode",
	"classify_intent",
	"detect_module",
	"detect_modules",
	"is_enabled",
]
