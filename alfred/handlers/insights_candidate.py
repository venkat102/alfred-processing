"""Heuristic report_candidate extraction for the Insights->Report handoff.

V1 is prompt-driven: parses the user's English to build a ReportCandidate.
Reply-driven extraction (having the Insights LLM emit structured JSON
alongside its markdown) would be more robust but meaningfully changes the
Insights prompt and carries parse-failure risk. Keeping V1 deterministic
so the handoff button appears predictably, then iterating on extraction
quality once usage data arrives.

Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
"""

from __future__ import annotations

import re

from alfred.models.insights_result import ReportCandidate

_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)

# Map phrases users write to Frappe's report time-range presets. Order
# matters for phrases that are substrings of each other (e.g. "this year"
# must come before "year") so the longer match wins.
_TIME_RANGE_PRESETS: tuple[tuple[str, str], ...] = (
	("today", "today"),
	("this week", "this_week"),
	("last week", "last_week"),
	("this month", "this_month"),
	("last month", "last_month"),
	("this quarter", "this_quarter"),
	("last quarter", "last_quarter"),
	("this year", "this_year"),
	("last year", "last_year"),
	("year to date", "year_to_date"),
	("ytd", "year_to_date"),
)

# Markers that indicate the Insights reply failed / returned nothing useful.
# When any is present in the reply we don't offer a Save as Report button -
# there's nothing to save.
_ERROR_REPLY_MARKERS: tuple[str, ...] = (
	"i don't know",
	"i do not know",
	"no data",
	"no matching",
	"couldn't find",
	"could not find",
	"not found",
	"no records",
)


def extract_report_candidate(*, prompt: str, reply: str) -> ReportCandidate | None:
	"""Return a ReportCandidate when the prompt is report-shaped, else None.

	Heuristic rules:
	  - Reply must not be obviously an error / empty-result message.
	  - Prompt must name a target entity we can map to a known DocType (via
	    the ModuleRegistry's target_doctype_matches).
	  - Prompt must carry at least one report-shape signal: a ``top N``
	    phrase, a time range, or both.
	"""
	if not prompt:
		return None

	low = prompt.lower()
	reply_low = (reply or "").lower()

	for marker in _ERROR_REPLY_MARKERS:
		if marker in reply_low:
			return None

	target = _detect_target_doctype(low)
	if target is None:
		return None

	# Top-N limit
	limit: int | None = None
	m = _TOP_N_RE.search(low)
	if m:
		limit = int(m.group(1))

	# Time range preset (longest phrase wins)
	time_range: dict | None = None
	for phrase, preset in _TIME_RANGE_PRESETS:
		if phrase in low:
			time_range = {"field": "posting_date", "preset": preset}
			break

	# Report-shape signal: need at least a limit or a time range. A prompt
	# like "what's customer X's credit limit" has a DocType (Customer) but
	# is not report-shaped - it's a scalar lookup.
	if limit is None and time_range is None:
		return None

	# Suggested name: "Top 10 Customers - This Quarter" style
	name_parts: list[str] = []
	if limit:
		name_parts.append(f"Top {limit}")
	name_parts.append(f"{target}s")
	if time_range:
		preset_h = time_range["preset"].replace("_", " ").title()
		name_parts.append(f"- {preset_h}")
	suggested_name = " ".join(name_parts)

	return ReportCandidate(
		target_doctype=target,
		report_type="Report Builder",
		limit=limit,
		time_range=time_range,
		suggested_name=suggested_name,
	)


def _detect_target_doctype(low_prompt: str) -> str | None:
	"""Scan registry target_doctype_matches for a verbatim hit; fall back to plural."""
	# Late import to avoid registry load at module import time in contexts
	# that don't need it (unit tests patching out the handler).
	from alfred.registry.module_loader import ModuleRegistry

	registry = ModuleRegistry.load()

	# Exact phrase first: "sales invoice", "customer" (as whole word)
	for kb in registry._by_module.values():
		for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
			if re.search(rf"\b{re.escape(dt.lower())}\b", low_prompt):
				return dt

	# Plural fallback: "customers" -> "Customer", "suppliers" -> "Supplier"
	for kb in registry._by_module.values():
		for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
			plural = dt.lower() + "s"
			if re.search(rf"\b{re.escape(plural)}\b", low_prompt):
				return dt

	return None
