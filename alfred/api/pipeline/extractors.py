"""Pure-function helpers + module-level constants for the pipeline
(TD-H2 PR 3 split from ``alfred/api/pipeline.py``).

Holds:
  - Warmup-cache state (`_WARMUP_CACHE`, `_PROBE_*`).
  - Drift-detection signals (`_DOCUMENTATION_MODE_PHRASES`,
    `_ERPNEXT_FIELD_SMELLS`, `_DOCTYPE_NAME_RE`, `_NON_DOCTYPE_CAPITALIZED`).
  - Site-state inject limits (`_INJECT_MAX_TARGETS`, `_INJECT_SITE_BUDGET`).
  - Report-handoff marker regex.
  - The pure helpers: ``_parse_report_candidate_marker``,
    ``_extract_target_doctypes``, ``_site_detail_has_artefacts``,
    ``_render_site_state_block``, ``_detect_drift``,
    ``_summarise_probe_error``.

Tests reach into ``_WARMUP_CACHE`` directly (clear between tests) and
patch module-level constants via the package path
(``alfred.api.pipeline._WARMUP_CACHE``); the package ``__init__``
re-exports every name here so those tests work unchanged.
"""

from __future__ import annotations

import json
import logging
import re as _re
from typing import Any

logger = logging.getLogger("alfred.pipeline")


# ── Warmup probe resilience ─────────────────────────────────────────
#
# Ollama reloads a model into VRAM on first request after an idle gap
# or when the tier roster changes. During that reload (a few seconds),
# a probe sees a 500 or a timeout even though Ollama is perfectly
# healthy a moment later. Per-prompt fail-fast on a single probe would
# abort the entire pipeline for what is a transient local warmup.
#
# Two levers to absorb that:
#   - `_WARMUP_CACHE` stamps `(model, base_url) -> monotonic-time` on
#     any successful probe within the last TTL, so follow-up prompts
#     don't re-probe a model we just talked to.
#   - `_PROBE_ATTEMPTS` gives each probe a small retry with backoff,
#     which covers the case where Ollama is mid-reload.
#
# Cache is process-local and cleared on failure of that tuple, so a
# truly sick Ollama still surfaces an error - we just stop paying the
# transient-reload tax on every prompt.
_WARMUP_CACHE: dict[tuple[str, str], float] = {}
_WARMUP_CACHE_TTL = 120.0
_PROBE_ATTEMPTS = 2
_PROBE_RETRY_BACKOFF_S = 3.0


# ── Drift detection (training-data bleed) ─────────────────────────────
#
# qwen2.5-coder:32b on Ollama sometimes slips out of the task structure
# when the prompt exceeds its effective attention budget and falls back
# to its training-data prior for Frappe. The most common drift is a
# verbose documentation dump of Sales Order (the most-cited DocType in
# its training corpus), delivered as prose with no JSON. When that
# happens we want to detect it BEFORE feeding the prose into extraction
# / rescue / the UI, so the user sees a specific, actionable error
# instead of a confusing wall of off-topic text.
#
# Signals of drift:
#   1. Output mentions a DocType (Title-Cased multi-word token) that
#      does NOT appear in the user's prompt.
#   2. Output uses "documentation mode" giveaway phrases like "The
#      provided JSON structure" or "describes the metadata".
#   3. Output is long prose with no JSON brackets at all.
#   4. Output contains ERPNext-specific field names the user never
#      mentioned (customer_name, taxes_and_charges, sales_team, etc.).
#
# Any one signal is enough to flag drift. We err on the side of false
# negatives - we don't want to flag a legit answer that happens to
# mention a related DocType. That's why we require the "foreign
# doctype" check to be combined with at least one doc-mode giveaway.

_DOCUMENTATION_MODE_PHRASES = (
	"the provided json structure",
	"the provided json",
	"describes the metadata",
	"here's a breakdown",
	"here is a breakdown",
	"the following json",
	"this json object",
	"document type:",  # markdown heading from doc-mode dumps
	"example usage",
)

# Field names that appear in ERPNext vanilla DocTypes and are a
# strong smell when mentioned without the user asking. These are
# training-data prior giveaways.
_ERPNEXT_FIELD_SMELLS = (
	"customer_name",
	"taxes_and_charges",
	"sales_team",
	"grand_total",
	"transaction_date",
	"delivery_date",
	"order_type",
)

_DOCTYPE_NAME_RE = _re.compile(r"\b([A-Z][a-z]+(?:\s[A-Z][a-z]+){0,3})\b")

# Cap on how many target DocTypes we'll deep-recon per turn. Users rarely
# ask about more than one in a single prompt; capping prevents a prompt
# that name-drops 6 DocTypes from triggering 6 MCP calls and blowing the
# inject-banner budget.
_INJECT_MAX_TARGETS = 2
# Max chars of site-state banner content per turn (not counting the
# decorative header/footer). At ~4 chars/token this is roughly 500 tokens.
_INJECT_SITE_BUDGET = 2000

# Words that look like DocType names by capitalization but aren't
# doctypes (common English nouns, section headers). Exclude them from
# the foreign-doctype check.
_NON_DOCTYPE_CAPITALIZED = frozenset({
	"The", "This", "That", "These", "Those", "An", "A", "It", "Its",
	"Here", "There", "What", "When", "Where", "Why", "How",
	"Module", "Fields", "Field", "Permissions", "Permission", "Example",
	"Conclusion", "Usage", "Type", "Types", "Name", "Names", "Label",
	"Notes", "Note", "Description", "Required", "Yes", "No", "Draft",
	"Submit", "Cancel", "Save", "New", "Create", "Read", "Write",
	"Delete", "Approve", "Reject", "Active", "Inactive",
	"Python", "JavaScript", "JSON", "HTML", "SQL",
	"Frappe", "ERPNext", "API",
	"System", "Manager", "User", "Admin", "Administrator",
	"Final", "Answer", "Thought", "Action", "Observation",
	"I", "You", "We",
	"North", "South", "East", "West",
	"Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
})


_REPORT_CANDIDATE_MARKER_RE = _re.compile(
	r"__report_candidate__\s*:\s*(\{[\s\S]*\})",
	_re.IGNORECASE,
)


def _parse_report_candidate_marker(prompt: str) -> dict | None:
	"""Return the JSON block attached to a prompt as a __report_candidate__ trailer.

	The alfred_client "Save as Report" button appends this marker so the
	pipeline can short-circuit intent classification to create_report with
	the already-resolved query shape. Returns None when no marker is found
	or the block fails to parse - caller falls back to normal classify.

	Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
	"""
	if not prompt:
		return None
	m = _REPORT_CANDIDATE_MARKER_RE.search(prompt)
	if not m:
		return None
	try:
		parsed = json.loads(m.group(1))
		if isinstance(parsed, dict):
			return parsed
	except json.JSONDecodeError:
		# Marker content isn't valid JSON (client bug / half-formed).
		# Fall back to the no-marker path; classify_intent runs normally.
		pass
	return None


def _extract_target_doctypes(prompt: str, limit: int = _INJECT_MAX_TARGETS) -> list[str]:
	"""Pull likely-target DocType names out of an enhanced prompt.

	Uses the same regex + noise-word filter as `_detect_drift` so extraction
	is consistent across the two call sites. A candidate is kept when:
	  - it isn't in _NON_DOCTYPE_CAPITALIZED (common English, section headers)
	  - it has a space (multi-word) OR is >= 6 chars (single-word DocTypes
	    like "Employee", "Customer", "ToDo" - shorter tokens are noise)
	  - it hasn't already been picked (dedup, first-occurrence-wins)

	Does NOT validate against the framework KG here - that's left to the
	site-detail MCP call, which already returns {error: not_found} for
	unknown DocTypes. Avoiding the extra MCP round-trip is important for
	pipeline latency (this runs on every Dev-mode turn).

	Returns up to `limit` candidates, preserving the order they appear in
	the prompt.
	"""
	if not prompt:
		return []
	picked: list[str] = []
	seen: set[str] = set()
	for cand in _DOCTYPE_NAME_RE.findall(prompt):
		if cand in seen:
			continue
		first_word = cand.split()[0]
		if first_word in _NON_DOCTYPE_CAPITALIZED:
			continue
		# Single-word candidate must be long enough to be a real DocType
		# name. "Draft", "Python" etc. are already in the exclude list; this
		# catches residual short capitalised words like "API" or "HR".
		if " " not in cand and len(cand) < 6:
			continue
		picked.append(cand)
		seen.add(cand)
		if len(picked) >= limit:
			break
	return picked


def _site_detail_has_artefacts(detail: dict) -> bool:
	"""True if a site-detail dict contains at least one artefact worth rendering.

	Prevents rendering a "SITE STATE FOR X" block for a DocType that exists
	but has zero customizations on this site - would just be banner noise.
	"""
	if not isinstance(detail, dict):
		return False
	for key in ("workflows", "server_scripts", "custom_fields", "notifications", "client_scripts"):
		if detail.get(key):
			return True
	return False


def _render_site_state_block(doctype: str, detail: dict, budget: int) -> str:
	"""Format one DocType's site-state into a compact banner block.

	Relevance order (highest-signal first so low-value artefacts get truncated
	when the budget runs low):
	  1. Workflows (graph is the most structural artefact)
	  2. Server Scripts (logic that might collide with user's request)
	  3. Custom Fields (schema extension)
	  4. Notifications (communication - lower priority for Dev mode)
	  5. Client Scripts (UI, lowest priority)

	`budget` is the max chars this function may emit (decorative header/footer
	excluded). As we render, we track cumulative length and stop adding new
	artefacts once the next one would push past the budget - callers get a
	"... (N more)" footer line so the agent knows there's unseen state.
	"""
	lines: list[str] = []
	remaining = budget
	truncated_kinds: list[tuple[str, int]] = []

	def _try_add(block: str) -> bool:
		nonlocal remaining
		if len(block) + 1 > remaining:
			return False
		lines.append(block)
		remaining -= len(block) + 1
		return True

	# Workflows - full graph
	for wf in detail.get("workflows") or []:
		states = wf.get("states") or []
		transitions = wf.get("transitions") or []
		state_line = " -> ".join(
			f"{s.get('state')} [{s.get('allow_edit') or '-'}]"
			for s in states
		) or "-"
		txn_summary = (
			", ".join(
				f"{t.get('state')} --{t.get('action')}--> {t.get('next_state')}"
				for t in transitions[:4]
			)
			+ (f" (+{len(transitions) - 4} more)" if len(transitions) > 4 else "")
		) if transitions else "no transitions"
		active = "active" if wf.get("is_active") else "inactive"
		block = (
			f"Workflow: {wf.get('name')} ({active}, field: "
			f"{wf.get('workflow_state_field') or '-'})\n"
			f"  states: {state_line}\n"
			f"  transitions: {txn_summary}"
		)
		if not _try_add(block):
			truncated_kinds.append(("workflow", 1))
			break

	# Server Scripts - body preview
	scripts = detail.get("server_scripts") or []
	# Active first, then disabled
	scripts = sorted(scripts, key=lambda s: int(s.get("disabled") or 0))
	for idx, s in enumerate(scripts):
		state = "disabled" if s.get("disabled") else "enabled"
		# Indent the body snippet so it reads as a nested block
		body = (s.get("script") or "").strip()
		body_indented = "\n".join("    " + ln for ln in body.splitlines()[:8]) or "    (empty)"
		block = (
			f"Server Script: {s.get('name')} "
			f"({s.get('doctype_event') or s.get('script_type') or '?'}, {state})\n"
			f"  body preview:\n{body_indented}"
		)
		if not _try_add(block):
			truncated_kinds.append(("server_script", len(scripts) - idx))
			break

	# Custom Fields - one line each
	fields = detail.get("custom_fields") or []
	if fields:
		field_lines: list[str] = []
		for f in fields:
			opt = f.get("options")
			ft = f.get("fieldtype")
			extra = f" (options: {opt.replace(chr(10), ',')})" if opt and ft in ("Select", "Link") else ""
			reqd = ", required" if f.get("reqd") else ""
			field_lines.append(
				f"  - {f.get('fieldname')} ({ft}{reqd}) label={f.get('label')!r}{extra}"
			)
		block = "Custom Fields:\n" + "\n".join(field_lines)
		if not _try_add(block):
			truncated_kinds.append(("custom_field", len(fields)))

	# Notifications
	notifs = detail.get("notifications") or []
	if notifs:
		notif_lines = [
			f"  - {n.get('name')} ({n.get('event')}, {n.get('channel')}): "
			f"{n.get('subject')!r}"
			for n in notifs
		]
		block = "Notifications:\n" + "\n".join(notif_lines)
		if not _try_add(block):
			truncated_kinds.append(("notification", len(notifs)))

	# Client Scripts - headline only
	clients = detail.get("client_scripts") or []
	if clients:
		client_lines = [
			f"  - {c.get('name')} (view: {c.get('view')}, "
			f"{'enabled' if c.get('enabled') else 'disabled'})"
			for c in clients
		]
		block = "Client Scripts:\n" + "\n".join(client_lines)
		if not _try_add(block):
			truncated_kinds.append(("client_script", len(clients)))

	if truncated_kinds:
		tail = ", ".join(f"{kind}: {n}" for kind, n in truncated_kinds)
		lines.append(f"(more artefacts omitted for brevity: {tail})")

	body = "\n\n".join(lines) if lines else "(no major artefacts)"
	return (
		f'=== SITE STATE FOR "{doctype}" (already on this site) ===\n'
		f"DO NOT propose anything that conflicts with or duplicates these "
		f"existing customizations. Extend, replace, or build atop them as "
		f"appropriate.\n\n"
		f"{body}\n"
		f"=========================================================="
	)


def _detect_drift(result_text: str, user_prompt: str) -> str | None:
	"""Return a short reason string if the output looks drifted, else None.

	Called by `_phase_post_crew` before extraction + rescue to catch
	training-data bleed and surface a specific error instead of the
	usual EMPTY_CHANGESET message.

	Happy-path short-circuit: the whole point of this check is to catch
	agents slipping into prose / documentation mode instead of emitting
	JSON. If the output is clearly a JSON array (the changeset shape
	Alfred expects), the agent did not drift into prose - downstream
	extraction handles malformed-JSON cases on its own. Skipping here
	prevents false positives where a specialist's legitimate string
	values (e.g. a Report changeset's `"report_type": "Report Builder"`
	plus a rationale mentioning "Query Report" / "Script Report") match
	the Title-Cased DocType-name regex and get flagged as foreign.
	"""
	if not result_text or not isinstance(result_text, str):
		return None
	stripped = result_text.strip()
	# Strip markdown code fences if present (`````json ... `````) so a
	# fenced-but-structurally-valid array still short-circuits.
	if stripped.startswith("```"):
		lines = stripped.splitlines()
		if lines and lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		stripped = "\n".join(lines).strip()
	if stripped.startswith("[") and stripped.endswith("]"):
		try:
			parsed = json.loads(stripped)
			if isinstance(parsed, list):
				return None
		except json.JSONDecodeError:
			# Malformed JSON falls through to drift checks below - if the
			# agent tried to emit a changeset but wrote unparseable JSON,
			# drift detection can still catch obvious prose mixed in.
			pass
	text = result_text.lower()
	prompt_lower = (user_prompt or "").lower()

	# Signal 1: ERPNext field smells the user never asked about
	for smell in _ERPNEXT_FIELD_SMELLS:
		if smell in text and smell not in prompt_lower:
			return f"output mentioned training-data field '{smell}' that the user never asked about"

	# Signal 2: "documentation mode" phrase
	doc_mode_hit = next(
		(p for p in _DOCUMENTATION_MODE_PHRASES if p in text),
		None,
	)

	# Signal 3: foreign DocType (Title-Cased multi-word token not in prompt)
	foreign_doctypes: list[str] = []
	if user_prompt:
		candidates = _DOCTYPE_NAME_RE.findall(result_text)
		for cand in candidates:
			# Filter out section headers, common English words, etc.
			first_word = cand.split()[0]
			if first_word in _NON_DOCTYPE_CAPITALIZED:
				continue
			if cand.lower() in prompt_lower:
				continue
			# Only count multi-word or clearly doctype-ish names. A single
			# capitalized word (e.g. "Draft") is too noisy.
			if " " in cand or len(cand) >= 6:
				foreign_doctypes.append(cand)

	# Combination rules
	if doc_mode_hit and foreign_doctypes:
		return (
			f"output slipped into documentation mode ('{doc_mode_hit}') about "
			f"{foreign_doctypes[0]!r} which is not in the user's request"
		)
	if doc_mode_hit and len(result_text) > 1500:
		return f"output is a long documentation dump containing '{doc_mode_hit}'"
	# A large prose output with no JSON at all is drift regardless
	if len(result_text) > 2000 and "{" not in result_text and "[" not in result_text:
		return "output is long prose with no JSON at all"
	# Too many foreign doctypes = the agent is clearly describing the
	# wrong thing even without a doc-mode giveaway
	if len(set(foreign_doctypes)) >= 3:
		return f"output references multiple unrelated doctypes: {sorted(set(foreign_doctypes))[:3]}"

	return None

def _summarise_probe_error(exc: Exception) -> str:
	"""Render a one-line reason for a warmup probe failure.

	Keeps the message short enough to fit a chat toast without leaking the
	full stack trace. HTTP status + body excerpt for HTTPError, bare str()
	for everything else.
	"""
	import urllib.error as _urllib_error

	if isinstance(exc, _urllib_error.HTTPError):
		try:
			body = (exc.read() or b"").decode(errors="replace")[:200]
		except (OSError, AttributeError):
			# OSError on socket-side body-read failure; AttributeError
			# if the HTTPError object has no readable body in this
			# urllib path. Keep "HTTP {code}" without the body suffix.
			body = ""
		return f"HTTP {exc.code}: {body}" if body else f"HTTP {exc.code}"
	return str(exc) or exc.__class__.__name__
