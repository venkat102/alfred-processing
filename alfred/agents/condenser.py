"""Handoff summary condenser for the CrewAI sequential pipeline.

CrewAI injects each task's full raw output as context into the next task.
Agents are verbose: they narrate schemas, recap decisions, and restate the
user's request before producing the structured JSON each downstream agent
actually needs. On the 6-agent SDLC crew that handoff context grows to
15-20k tokens by the Deployer, which inflates every LLM call and slows
local Ollama runs.

This module wires a Task.callback that fires right after each upstream task
completes and rewrites `task_output.raw` in place. CrewAI's formatter
(`aggregate_raw_outputs_from_task_outputs`) reads `output.raw` lazily when
building the next task's context, so the mutation propagates without
touching CrewAI internals.

Strategy for each output:
  1. Strip markdown code fences.
  2. Try to parse the whole thing as JSON. If it parses, re-emit as compact
     JSON (no indent, shortest separators).
  3. Otherwise locate the outermost balanced {...} or [...] substring and
     parse that. If it parses, compact-emit it.
  4. Final fallback: tail-truncate to _MAX_FALLBACK_CHARS (agents tend to
     put their conclusion at the bottom, so tail preserves the most useful
     signal when JSON extraction fails entirely).

Importantly, we do NOT touch `generate_changeset` output. Its raw form is
read by `run_crew` to extract the final changeset for the preview, and by
the Tester/Deployer as-is. Condensing it would risk dropping items.
"""

import json
import logging
import re

logger = logging.getLogger("alfred.condenser")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

# Tail-truncation cap for outputs where JSON extraction fails. 1500 chars
# is ~375 tokens - enough to keep the agent's final recap, small enough
# that the next agent's context doesn't balloon from a prose-heavy predecessor.
_MAX_FALLBACK_CHARS = 1500

# Tasks whose raw output must NOT be rewritten. generate_changeset is the
# final artifact extracted by run_crew. validate_changeset and deploy_changeset
# are terminal and not read as context by anything downstream.
_SKIP_CONDENSE = {"generate_changeset", "validate_changeset", "deploy_changeset"}


def condense_raw_output(task_name: str, raw: str) -> str:
	"""Return a compact form of the task's raw output.

	Falls through in order: compact-JSON, extracted-JSON substring,
	tail-truncation. Always returns a string - never raises.
	"""
	if raw is None:
		return ""
	if not isinstance(raw, str):
		raw = str(raw)
	text = raw.strip()
	if not text:
		return text

	fence_match = _FENCE_RE.search(text)
	if fence_match:
		text = fence_match.group(1).strip()

	parsed = _try_parse_json(text)
	if parsed is not None:
		return json.dumps(parsed, separators=(",", ":"))

	candidate = _find_outermost_json(text)
	if candidate:
		parsed = _try_parse_json(candidate)
		if parsed is not None:
			return json.dumps(parsed, separators=(",", ":"))

	if len(text) > _MAX_FALLBACK_CHARS:
		return "... (truncated) ...\n" + text[-_MAX_FALLBACK_CHARS:]
	return text


def _try_parse_json(text: str):
	try:
		return json.loads(text)
	except (json.JSONDecodeError, ValueError):
		return None


def _find_outermost_json(text: str) -> str | None:
	"""Return the first balanced JSON object or array substring, or None.

	Walks the string once, tracking brace/bracket depth and string state so
	we don't get fooled by `{` inside a quoted value. Handles both `{...}`
	and `[...]` top-level shapes. Picks whichever opens first.
	"""
	start_obj = text.find("{")
	start_arr = text.find("[")
	candidates = [c for c in (start_obj, start_arr) if c != -1]
	if not candidates:
		return None
	start = min(candidates)
	open_char = text[start]
	close_char = "}" if open_char == "{" else "]"

	depth = 0
	in_string = False
	escape = False
	for i in range(start, len(text)):
		ch = text[i]
		if escape:
			escape = False
			continue
		if ch == "\\":
			escape = True
			continue
		if ch == '"':
			in_string = not in_string
			continue
		if in_string:
			continue
		if ch == open_char:
			depth += 1
		elif ch == close_char:
			depth -= 1
			if depth == 0:
				return text[start : i + 1]
	return None


def make_condenser_callback(task_name: str):
	"""Return a Task.callback that rewrites task_output.raw to a compact form.

	If condensation fails for any reason the original output is preserved,
	so a buggy condenser can't break the pipeline - worst case is no speedup.
	"""
	if task_name in _SKIP_CONDENSE:
		return None

	def _callback(task_output):
		try:
			raw = getattr(task_output, "raw", None)
			if not raw or not isinstance(raw, str):
				return task_output
			original_len = len(raw)
			condensed = condense_raw_output(task_name, raw)
			condensed_len = len(condensed)
			if condensed_len >= original_len:
				return task_output
			task_output.raw = condensed
			reduction = 100 * (1 - condensed_len / original_len)
			logger.info(
				"Condensed %s output: %d -> %d chars (-%.0f%%)",
				task_name, original_len, condensed_len, reduction,
			)
		except Exception as e:
			logger.warning(
				"Condenser for %s failed, preserving original: %s", task_name, e
			)
		return task_output

	return _callback
