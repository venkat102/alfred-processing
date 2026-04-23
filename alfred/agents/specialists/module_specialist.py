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
			except Exception as e:  # noqa: BLE001
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
	except Exception:  # noqa: BLE001
		pass

	decoder = json.JSONDecoder()
	for idx, ch in enumerate(cleaned):
		if ch != "[":
			continue
		try:
			parsed, _ = decoder.raw_decode(cleaned[idx:])
			if isinstance(parsed, list):
				return parsed
		except Exception:  # noqa: BLE001
			continue
	return None


_CONTEXT_CACHE_TTL_SECONDS = 300  # 5 minutes, per spec

# Families change far less frequently than per-module conventions, so
# we cache the family-level snippet for longer. 15 minutes keeps the LLM
# call rate low without making iterations on family KBs painful.
_FAMILY_CONTEXT_CACHE_TTL_SECONDS = 900

# Process-local fallback cache. Used when the pipeline cannot supply a
# Redis client (local dev without Redis, Redis reachability loss mid-run).
# Redis is the primary backend and lets multiple workers share a hit.
_context_cache: dict[tuple[str, str, str], tuple[float, str]] = {}

# Family cache keyed on (family_name, intent). Family snippets don't
# vary by target_doctype - they're cross-module invariants that apply
# to the whole family regardless of the specific DocType in play.
_family_context_cache: dict[tuple[str, str], tuple[float, str]] = {}


def _cache_key_str(module: str, intent: str, target_doctype: str | None) -> str:
	return f"alfred:module_ctx:{module}:{intent}:{target_doctype or ''}"


def _family_cache_key_str(family: str, intent: str) -> str:
	return f"alfred:family_ctx:{family}:{intent}"


def _context_cache_get_inmem(key: tuple[str, str, str]) -> str | None:
	entry = _context_cache.get(key)
	if entry is None:
		return None
	expires_at, value = entry
	if time.time() > expires_at:
		_context_cache.pop(key, None)
		return None
	return value


def _context_cache_set_inmem(key: tuple[str, str, str], value: str) -> None:
	_context_cache[key] = (time.time() + _CONTEXT_CACHE_TTL_SECONDS, value)


async def _context_cache_get_redis(redis, key_str: str) -> str | None:
	try:
		return await redis.get(key_str)
	except Exception as e:  # noqa: BLE001
		logger.debug("Module context cache Redis read failed: %s", e)
		return None


async def _context_cache_set_redis(redis, key_str: str, value: str) -> None:
	try:
		await redis.setex(key_str, _CONTEXT_CACHE_TTL_SECONDS, value)
	except Exception as e:  # noqa: BLE001
		logger.debug("Module context cache Redis write failed: %s", e)


def _context_cache_clear() -> None:
	"""Test helper - clears the in-memory caches (module + family).

	Redis cache is not cleared here because tests that use Redis mock it
	explicitly. For real Redis-connected runs, entries expire via TTL.
	"""
	_context_cache.clear()
	_family_context_cache.clear()


def _family_cache_get_inmem(key: tuple[str, str]) -> str | None:
	entry = _family_context_cache.get(key)
	if entry is None:
		return None
	expires_at, value = entry
	if time.time() > expires_at:
		_family_context_cache.pop(key, None)
		return None
	return value


def _family_cache_set_inmem(key: tuple[str, str], value: str) -> None:
	_family_context_cache[key] = (
		time.time() + _FAMILY_CONTEXT_CACHE_TTL_SECONDS, value,
	)


async def _family_cache_get_redis(redis, key_str: str) -> str | None:
	try:
		return await redis.get(key_str)
	except Exception as e:  # noqa: BLE001
		logger.debug("Family context cache Redis read failed: %s", e)
		return None


async def _family_cache_set_redis(redis, key_str: str, value: str) -> None:
	try:
		await redis.setex(key_str, _FAMILY_CONTEXT_CACHE_TTL_SECONDS, value)
	except Exception as e:  # noqa: BLE001
		logger.debug("Family context cache Redis write failed: %s", e)


async def provide_family_context(
	*,
	family: str,
	intent: str,
	site_config: dict,
	redis=None,
) -> str:
	"""Family pre-pass. Returns a prompt snippet for the family-level context.

	Summarises the cross_module_invariants + backstory of a family KB
	(one of transactions / operations / people / engagement) relevant
	to the current intent. The snippet is prepended to the per-module
	snippet in the pipeline so intent specialists see both layers.

	Cache: same Redis + in-memory shape as ``provide_context`` but
	keyed on (family, intent) and with a 15-minute TTL. Family KBs
	change much less than module KBs.
	"""
	try:
		kb = ModuleRegistry.load().get_family(family)
	except KeyError:
		return ""

	inmem_key = (family, intent)
	redis_key = _family_cache_key_str(family, intent)

	if redis is not None:
		cached = await _family_cache_get_redis(redis, redis_key)
		if cached is not None:
			logger.info(
				"Family context provided: family=%s intent=%s cached=redis chars=%d",
				family, intent, len(cached),
			)
			return cached
	cached = _family_cache_get_inmem(inmem_key)
	if cached is not None:
		logger.info(
			"Family context provided: family=%s intent=%s cached=inmem chars=%d",
			family, intent, len(cached),
		)
		return cached

	invariants = kb.get("cross_module_invariants", [])
	invariants_block = "\n".join(f"- {inv}" for inv in invariants)
	user_msg = (
		f"Intent: {intent}\n"
		f"Family cross-module invariants:\n{invariants_block}\n\n"
		"Summarise the subset of these family-level invariants that matter "
		"for this intent. 3-5 sentences, concrete, no prose intro, no JSON, "
		"no markdown headers. These are cross-module rules shared by every "
		"member module of the family."
	)

	try:
		reply = await _ollama_chat(
			messages=[
				{"role": "system", "content": kb["backstory"]},
				{"role": "user", "content": user_msg},
			],
			site_config=site_config,
			tier=site_config.get("llm_tier", "triage"),
			max_tokens=350,
			temperature=0.2,
		)
		result = (reply or "").strip()
		if result:
			if redis is not None:
				await _family_cache_set_redis(redis, redis_key, result)
			_family_cache_set_inmem(inmem_key, result)
			logger.info(
				"Family context provided: family=%s intent=%s cached=miss chars=%d",
				family, intent, len(result),
			)
		else:
			logger.info(
				"Family context provided: family=%s intent=%s cached=miss chars=0 (empty reply)",
				family, intent,
			)
		return result
	except Exception as e:  # noqa: BLE001
		logger.warning("Family specialist provide_family_context failed (%s): %s", family, e)
		return ""


async def provide_context(
	*,
	module: str,
	intent: str,
	target_doctype: str | None,
	site_config: dict,
	redis=None,
) -> str:
	"""Context pre-pass. Returns a prompt snippet for the intent specialist.

	Cache backend selection:
	  - When ``redis`` is provided and healthy, the cache is shared across
	    workers and persists across process restarts (TTL 5 minutes).
	  - Otherwise, the process-local in-memory cache is used - still
	    avoids duplicate LLM calls within a single worker's lifetime.

	Key format: ``alfred:module_ctx:<module>:<intent>:<target_doctype>``.
	"""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return ""

	inmem_key = (module, intent, target_doctype or "")
	redis_key = _cache_key_str(module, intent, target_doctype)

	# Cache read: prefer Redis if available, fall back to in-memory.
	if redis is not None:
		cached = await _context_cache_get_redis(redis, redis_key)
		if cached is not None:
			logger.info(
				"Module context provided: module=%s intent=%s target=%r cached=redis chars=%d",
				module, intent, target_doctype, len(cached),
			)
			return cached
	cached = _context_cache_get_inmem(inmem_key)
	if cached is not None:
		logger.info(
			"Module context provided: module=%s intent=%s target=%r cached=inmem chars=%d",
			module, intent, target_doctype, len(cached),
		)
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
			# Write-through: Redis when available (so other workers hit it),
			# plus in-memory (so this worker's next hit skips Redis roundtrip).
			if redis is not None:
				await _context_cache_set_redis(redis, redis_key, result)
			_context_cache_set_inmem(inmem_key, result)
			logger.info(
				"Module context provided: module=%s intent=%s target=%r cached=miss chars=%d",
				module, intent, target_doctype, len(result),
			)
		else:
			logger.info(
				"Module context provided: module=%s intent=%s target=%r cached=miss chars=0 (empty reply)",
				module, intent, target_doctype,
			)
		return result
	except Exception as e:  # noqa: BLE001
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
	except Exception as e:  # noqa: BLE001
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

	rule_count = len(notes)
	llm_added = 0
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
			llm_added += 1
		except Exception as e:  # noqa: BLE001
			logger.debug("Dropping malformed LLM note %r: %s", entry, e)

	logger.info(
		"Module validation ran: module=%s intent=%s items=%d rule_notes=%d llm_notes=%d",
		module, intent, len(changes), rule_count, llm_added,
	)
	return notes


def _normalise_issue(text: str) -> str:
	"""Lowercase + collapse whitespace so dedup ignores cosmetic differences."""
	if not text:
		return ""
	return " ".join(text.lower().split())


def cap_secondary_severity(notes: list[ValidationNote]) -> list[ValidationNote]:
	"""Return copies of notes with blocker severity capped to warning.

	Secondary modules in the V3 multi-module pipeline cannot gate deploy:
	a blocker from a secondary-context specialist becomes a warning in
	the merged notes list. Primary-module notes keep full severity.

	Spec: docs/specs/2026-04-22-multi-module-classification.md.
	"""
	out: list[ValidationNote] = []
	for n in notes:
		if n.severity == "blocker":
			out.append(ValidationNote(
				severity="warning",
				source=n.source,
				issue=n.issue,
				field=n.field,
				fix=n.fix,
				changeset_index=n.changeset_index,
			))
		else:
			out.append(n)
	return out
