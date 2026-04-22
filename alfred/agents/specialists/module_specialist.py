"""Module specialist - invoked twice per Dev-mode build.

- provide_context(module, intent, target_doctype, site_config)
      LLM call at the start of build: returns a prompt snippet summarising
      module-specific conventions relevant to the intent.

- validate_output(module, intent, changes, site_config)
      LLM call at the end of build: returns a list of ValidationNotes where
      module conventions were dropped, contradicted, or misapplied.

This file ships in two phases:
  Task 6: deterministic rule runner (run_rule_validation). No LLM.
  Task 7: LLM-backed provide_context + validate_output that wrap the rule
          runner and merge its notes with LLM-discovered notes.

Spec: docs/specs/2026-04-22-module-specialists.md.
"""

from __future__ import annotations

import logging

from alfred.models.agent_outputs import ValidationNote
from alfred.registry.module_loader import ModuleRegistry, UnknownModuleError

logger = logging.getLogger("alfred.agents.specialists.module")


def _rule_applies(when: dict, item: dict) -> bool:
	"""Check the rule's ``when`` clause against a single changeset item.

	The ``when`` clause is a dict of dotted-path keys to expected values.
	Examples:
	  {"doctype": "DocType"}                         -> item["doctype"] == "DocType"
	  {"doctype": "DocType", "data.is_submittable": 1} -> both must hold.
	"""
	for key, expected in when.items():
		actual = item
		for part in key.split("."):
			if not isinstance(actual, dict):
				return False
			actual = actual.get(part)
		if actual != expected:
			return False
	return True


def run_rule_validation(
	*, module: str, changes: list[dict],
) -> list[ValidationNote]:
	"""Apply a module's declared validation_rules to each change item.

	Deterministic; no LLM. Returns a list of ValidationNotes. Unknown
	module or empty changes -> empty list. Individual rule failures are
	caught and logged so a malformed rule doesn't block the pipeline.
	"""
	if not changes:
		return []

	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return []

	notes: list[ValidationNote] = []
	for idx, item in enumerate(changes):
		for rule in kb.get("validation_rules", []):
			try:
				if _rule_applies(rule["when"], item):
					notes.append(ValidationNote(
						severity=rule["severity"],
						source=f"module_rule:{rule['id']}",
						issue=rule["message"],
						fix=rule.get("fix"),
						changeset_index=idx,
					))
			except Exception as e:
				logger.warning(
					"Module rule %s failed while evaluating change %d: %s",
					rule.get("id", "<unknown>"), idx, e,
				)
	return notes
