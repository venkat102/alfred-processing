"""Minimality reflection step for the Alfred pipeline.

After the Developer agent has produced a changeset but before dry-run, a small
LLM call reviews every item against the original user request and asks: "Is
this item strictly necessary to satisfy what the user asked for?" Items the
reviewer flags are dropped before dry-run.

The motivation is over-reach: local coder models (qwen, llama) often produce
a notification PLUS an audit log PLUS a related custom field when the user
asked only for the notification. The extra items sometimes break dry-run
validation, and even when they don't they annoy the user and clutter the
preview. A conservative reviewer catches the obvious cases (err on the side
of keeping things) without touching the legitimate core.

Design rules:
  - Deterministic parsing: the reviewer must return a raw JSON array of
    integer indices, nothing else. Parse with `_parse_indices_strict`.
  - Bounded: exactly one reflection call per pipeline. No retry loops.
  - Fail-safe: on any parse error, timeout, or LLM failure the changeset
    passes through unchanged. Silent no-op on any edge case.
  - Never strip everything: if the reviewer flags every index we log a
    warning and keep everything, because that almost certainly means the
    reviewer misunderstood the request rather than "the whole thing is
    over-reach".
  - Feature-flagged via ALFRED_REFLECTION_ENABLED so it can be toggled off
    without a code change if behaviour regresses.

Not in scope:
  - Editing or fixing items. Reflection only removes, never rewrites.
  - Multi-round critique. One pass and done.
  - Reasoning about dependencies between items. That's the Architect's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

logger = logging.getLogger("alfred.reflection")

_SYSTEM_PROMPT = """\
You are a minimality reviewer for a Frappe customization pipeline.

You receive the USER'S ORIGINAL REQUEST and a CHANGESET (a JSON array of
documents the Developer proposes to create or update). Your job is to
identify items that are NOT strictly needed to satisfy the user's request.

RULES:
- Err on the side of KEEPING items. Default answer is an empty list [].
- Only flag items that are clearly NOT in the user's request and clearly
  NOT required for the requested items to function. Things to flag:
  - Extra audit log DocTypes the user didn't ask for.
  - Extra notifications for events the user didn't mention.
  - Extra custom fields that aren't referenced by any other item.
  - Extra server scripts for validations the user didn't mention.
- Things to KEEP even if they feel like extras:
  - Custom fields that a notification or script directly references.
  - Child-table rows inside a workflow (states, transitions).
  - Permission rows, role assignments, naming-series settings - these
    come with DocType creation.
- If you are unsure, KEEP the item.
- Never return all indices. If every item looks unnecessary, return [].

OUTPUT FORMAT (STRICT):
Return a single raw JSON object on one line:
{"remove": [<int>, <int>, ...], "reasons": ["<short reason>", "<short reason>"]}
- "remove" is the list of 0-based item indices to drop.
- "reasons" is the same length as "remove", with a one-sentence reason
  per dropped item.
- If nothing should be removed, return {"remove": [], "reasons": []}.
- No prose, no markdown, no code fences. JSON only."""


def _reflection_enabled() -> bool:
	"""Feature flag check. Default off for cautious rollout."""
	return os.environ.get("ALFRED_REFLECTION_ENABLED", "").lower() in {"1", "true", "yes"}


def _describe_item(item: dict) -> str:
	"""Produce a one-line description of a changeset item for the prompt."""
	if not isinstance(item, dict):
		return str(item)[:80]
	op = item.get("op") or "create"
	doctype = item.get("doctype") or (item.get("data") or {}).get("doctype") or "?"
	data = item.get("data") or {}
	name = data.get("name") or data.get("fieldname") or data.get("label") or "?"
	# Include the most identifying hint per doctype for the reviewer.
	hint = ""
	if doctype == "Notification":
		hint = f" on {data.get('document_type', '?')} / event={data.get('event', '?')}"
	elif doctype == "Custom Field":
		hint = f" on {data.get('dt', '?')} / fieldtype={data.get('fieldtype', '?')}"
	elif doctype == "Server Script":
		hint = f" on {data.get('reference_doctype', '?')} / event={data.get('doctype_event', '?')}"
	elif doctype == "Workflow":
		hint = f" on {data.get('document_type', '?')}"
	return f"{op} {doctype} '{name}'{hint}"


def _parse_indices_strict(raw: str, n_items: int) -> tuple[list[int], list[str]]:
	"""Parse the reviewer's JSON response into (indices, reasons).

	Returns empty lists on any parse failure - reflection is non-critical,
	so we silently no-op rather than blocking the pipeline.
	"""
	if not raw:
		return [], []
	text = raw.strip()
	# Strip code fences in case the model added them despite instructions.
	text = re.sub(r"^```(?:json)?\s*", "", text)
	text = re.sub(r"\s*```$", "", text)
	try:
		parsed = json.loads(text)
	except json.JSONDecodeError:
		# Try to locate the first `{...}` block and parse just that.
		match = re.search(r"\{.*\}", text, re.DOTALL)
		if not match:
			return [], []
		try:
			parsed = json.loads(match.group())
		except json.JSONDecodeError:
			return [], []
	if not isinstance(parsed, dict):
		return [], []
	raw_indices = parsed.get("remove") or []
	raw_reasons = parsed.get("reasons") or []
	if not isinstance(raw_indices, list):
		return [], []
	# Filter to valid indices in range
	indices: list[int] = []
	for v in raw_indices:
		try:
			idx = int(v)
		except (TypeError, ValueError):
			continue
		if 0 <= idx < n_items:
			indices.append(idx)
	# Deduplicate while preserving order
	seen = set()
	deduped = []
	for idx in indices:
		if idx not in seen:
			seen.add(idx)
			deduped.append(idx)
	indices = deduped
	# Align reasons with indices (pad/truncate as needed)
	reasons = [str(r) for r in raw_reasons if r]
	while len(reasons) < len(indices):
		reasons.append("not strictly required by the user's request")
	reasons = reasons[: len(indices)]
	return indices, reasons


async def reflect_minimality(
	original_prompt: str,
	changeset: list[dict],
	site_config: dict,
) -> tuple[list[dict], list[dict]]:
	"""Run the minimality review and return (kept_items, removed_items).

	removed_items is a list of {"index": int, "item": dict, "reason": str}
	so callers can log each drop and surface it in the UI.

	Never raises. On any failure the changeset passes through unchanged
	and removed_items is [].
	"""
	if not _reflection_enabled():
		return changeset, []
	if not changeset or not original_prompt:
		return changeset, []
	if len(changeset) < 2:
		# Single-item changesets have nothing to prune. The reviewer would
		# never flag the only item as overreach.
		return changeset, []

	try:
		from alfred.llm_client import ollama_chat

		items_text = "\n".join(
			f"  [{i}] {_describe_item(item)}" for i, item in enumerate(changeset)
		)
		user_msg = (
			f"USER REQUEST:\n{original_prompt[:3000]}\n\n"
			f"CHANGESET ({len(changeset)} items):\n{items_text}\n\n"
			"Which items (if any) are NOT strictly needed? Return the JSON object."
		)

		raw = await ollama_chat(
			messages=[
				{"role": "system", "content": _SYSTEM_PROMPT},
				{"role": "user", "content": user_msg},
			],
			site_config=site_config,
			tier="triage",
			max_tokens=256,
			temperature=0.0,
			num_ctx_override=8192,
			timeout=30,
		)
		logger.info("Reflection raw response (first 300): %r", (raw or "")[:300])

		indices, reasons = _parse_indices_strict(raw, len(changeset))
		if not indices:
			return changeset, []

		# Safety net: never strip everything. If the reviewer flagged the
		# entire changeset, keep everything and log loudly - that's almost
		# always a misread of the request.
		if len(indices) >= len(changeset):
			logger.warning(
				"Reflection flagged all %d items; keeping everything. "
				"Indices: %s, reasons: %s",
				len(changeset), indices, reasons,
			)
			return changeset, []

		removed_set = set(indices)
		kept: list[dict] = []
		removed: list[dict] = []
		for i, item in enumerate(changeset):
			if i in removed_set:
				pos = indices.index(i)
				reason = reasons[pos] if pos < len(reasons) else "not strictly needed"
				removed.append({"index": i, "item": item, "reason": reason})
			else:
				kept.append(item)

		logger.info(
			"Reflection dropped %d/%d items: %s",
			len(removed), len(changeset),
			[(r["index"], r["reason"]) for r in removed],
		)
		return kept, removed
	except Exception as e:
		logger.warning("Reflection step failed, changeset passes through: %s", e)
		return changeset, []
