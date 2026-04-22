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

import json
import logging
import time

from alfred.llm_client import ollama_chat as _ollama_chat
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


def _strip_code_fences(text: str) -> str:
	if not text:
		return text
	cleaned = text.strip()
	if cleaned.startswith("```"):
		lines = cleaned.splitlines()
		if lines and lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		cleaned = "\n".join(lines).strip()
	return cleaned


def _parse_llm_note_list(raw: str) -> list[dict] | None:
	"""Parse the LLM's validation response as a JSON array of note dicts.

	Mirrors handlers/plan.py's _parse_plan_doc_json robustness. Returns
	None on unrecoverable parse failure so callers can fall back to
	rule-only notes.
	"""
	if not raw:
		return None
	cleaned = _strip_code_fences(raw)
	try:
		parsed = json.loads(cleaned)
		if isinstance(parsed, list):
			return parsed
	except Exception:
		pass

	decoder = json.JSONDecoder()
	for idx, ch in enumerate(cleaned):
		if ch != "[":
			continue
		try:
			parsed, _ = decoder.raw_decode(cleaned[idx:])
			if isinstance(parsed, list):
				return parsed
		except Exception:
			continue
	return None


_CONTEXT_CACHE_TTL_SECONDS = 300  # 5 minutes, per spec
_context_cache: dict[tuple[str, str, str], tuple[float, str]] = {}


def _context_cache_get(key: tuple[str, str, str]) -> str | None:
	entry = _context_cache.get(key)
	if entry is None:
		return None
	expires_at, value = entry
	if time.time() > expires_at:
		_context_cache.pop(key, None)
		return None
	return value


def _context_cache_set(key: tuple[str, str, str], value: str) -> None:
	_context_cache[key] = (time.time() + _CONTEXT_CACHE_TTL_SECONDS, value)


def _context_cache_clear() -> None:
	"""Test helper - clears the in-memory cache."""
	_context_cache.clear()


async def provide_context(
	*,
	module: str,
	intent: str,
	target_doctype: str | None,
	site_config: dict,
) -> str:
	"""Context pre-pass. Returns a prompt snippet for the intent specialist.

	Process-local cache by (module, intent, target_doctype) with 5-minute
	TTL avoids a fresh LLM call for every Dev-mode build that targets the
	same DocType. Invalidated naturally when the processing app restarts.
	"""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return ""

	cache_key = (module, intent, target_doctype or "")
	cached = _context_cache_get(cache_key)
	if cached is not None:
		return cached

	user_parts = [f"Intent: {intent}"]
	if target_doctype:
		user_parts.append(f"Target DocType: {target_doctype}")
	user_parts.append(
		"Summarise the subset of your module knowledge relevant to this "
		"intent + target. Output should be 3-6 sentences of concrete "
		"conventions, role names, and gotchas. No prose introduction, no "
		"JSON, no markdown headers."
	)

	try:
		reply = await _ollama_chat(
			messages=[
				{"role": "system", "content": kb["backstory"]},
				{"role": "user", "content": "\n".join(user_parts)},
			],
			site_config=site_config,
			tier=site_config.get("llm_tier", "triage"),
			max_tokens=400,
			temperature=0.2,
		)
		result = (reply or "").strip()
		if result:
			_context_cache_set(cache_key, result)
		return result
	except Exception as e:
		logger.warning("Module specialist provide_context failed (%s): %s", module, e)
		return ""


async def validate_output(
	*,
	module: str,
	intent: str,
	changes: list[dict],
	site_config: dict,
) -> list[ValidationNote]:
	"""Validation post-pass. Merges deterministic rule notes with LLM notes."""
	if not changes:
		return []

	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return []

	# Rule-runner half (deterministic, always runs)
	notes = run_rule_validation(module=module, changes=changes)

	prompt_body = (
		f"Intent: {intent}\n"
		f"Changeset (JSON):\n{json.dumps(changes, indent=2)[:4000]}\n\n"
		"Review the changeset against your module's conventions. Emit a "
		"JSON array of notes where domain conventions have been dropped, "
		"contradicted, or misapplied. Each note has keys: severity "
		"(advisory|warning|blocker), issue (short string), field (optional "
		"dotted path), fix (optional string). Output ONLY the JSON array. "
		"If nothing is wrong, output: []"
	)

	try:
		raw = await _ollama_chat(
			messages=[
				{"role": "system", "content": kb["backstory"]},
				{"role": "user", "content": prompt_body},
			],
			site_config=site_config,
			tier=site_config.get("llm_tier", "triage"),
			max_tokens=600,
			temperature=0.1,
		)
	except Exception as e:
		logger.warning("Module specialist validate_output LLM failed (%s): %s", module, e)
		return notes

	parsed = _parse_llm_note_list(raw or "")
	if parsed is None:
		logger.warning(
			"Module specialist validate_output returned unparseable JSON (first 300): %r",
			(raw or "")[:300],
		)
		return notes

	# Dedup LLM notes against rule notes by normalised issue text. If
	# the LLM surfaces the same concern the rule runner already caught
	# (different source but same message), skip - don't show the user
	# the same thing twice.
	rule_issues = {_normalise_issue(n.issue) for n in notes}

	for entry in parsed:
		if not isinstance(entry, dict):
			continue
		issue_text = entry.get("issue", "")
		if _normalise_issue(issue_text) in rule_issues:
			continue
		try:
			notes.append(ValidationNote(
				severity=entry.get("severity", "advisory"),
				source=f"module_specialist:{module}",
				issue=issue_text,
				field=entry.get("field"),
				fix=entry.get("fix"),
			))
		except Exception as e:
			logger.debug("Dropping malformed LLM note %r: %s", entry, e)

	return notes


def _normalise_issue(text: str) -> str:
	"""Lowercase + collapse whitespace so dedup ignores cosmetic differences."""
	if not text:
		return ""
	return " ".join(text.lower().split())
