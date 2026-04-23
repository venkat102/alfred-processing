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
from datetime import date, timedelta

from alfred.models.insights_result import ReportCandidate

_TOP_N_RE = re.compile(r"\btop\s+(\d+)\b", re.IGNORECASE)

# ── Aggregation detection ──────────────────────────────────────────
# ``top N <entity> by <metric> <time>`` and ``<metric> by <entity>
# <time>`` are shapes Report Builder cannot satisfy - they need
# GROUP BY + SUM, which is Query Report territory. When we detect an
# aggregation pattern we emit a Query Report candidate with a
# ready-made SQL body so the specialist can copy it verbatim.

# Metric phrase -> (source DocType, metric column, SQL fn, human label).
# ``source DocType`` is where the metric LIVES, which becomes
# ``ref_doctype`` on the Report. For revenue that's Sales Invoice
# (grand_total); Customer has no revenue column, so a Report Builder
# over Customer would have nothing to aggregate.
# Longer phrases first so "billed amount" wins over "billing".
_METRIC_PHRASES: tuple[tuple[str, tuple[str, str, str, str]], ...] = (
	("billed amount", ("Sales Invoice", "grand_total", "SUM", "Billed Amount")),
	("purchase amount", ("Purchase Invoice", "grand_total", "SUM", "Purchase Amount")),
	("purchase value", ("Purchase Invoice", "grand_total", "SUM", "Purchase Value")),
	("sales value", ("Sales Invoice", "grand_total", "SUM", "Sales Value")),
	("revenue", ("Sales Invoice", "grand_total", "SUM", "Revenue")),
	("billing", ("Sales Invoice", "grand_total", "SUM", "Billing")),
	("sales", ("Sales Invoice", "grand_total", "SUM", "Sales")),
	("purchases", ("Purchase Invoice", "grand_total", "SUM", "Purchases")),
	("spend", ("Purchase Invoice", "grand_total", "SUM", "Spend")),
)

# Group-by entity phrase -> (field name on source DocType, human label).
# ERPNext-standard field names on Sales Invoice / Purchase Invoice.
# Longer phrases first ("sales person" before "sales").
_GROUP_BY_PHRASES: tuple[tuple[str, tuple[str, str]], ...] = (
	("sales persons", ("sales_person", "Sales Person")),
	("sales people", ("sales_person", "Sales Person")),
	("sales person", ("sales_person", "Sales Person")),
	("salespersons", ("sales_person", "Sales Person")),
	("salesperson", ("sales_person", "Sales Person")),
	("territories", ("territory", "Territory")),
	("territory", ("territory", "Territory")),
	("customer groups", ("customer_group", "Customer Group")),
	("customer group", ("customer_group", "Customer Group")),
	("customers", ("customer", "Customer")),
	("customer", ("customer", "Customer")),
	("suppliers", ("supplier", "Supplier")),
	("supplier", ("supplier", "Supplier")),
	("items", ("item_code", "Item")),
	("item", ("item_code", "Item")),
	("products", ("item_code", "Item")),
	("product", ("item_code", "Item")),
	("projects", ("project", "Project")),
	("project", ("project", "Project")),
)


def _resolve_preset_range(preset: str, today: date | None = None) -> tuple[date, date] | None:
	"""Resolve a time-range preset keyword to (start_date, end_date).

	Returns None when the preset is unknown. Pure function; ``today``
	can be injected in tests.
	"""
	t = today or date.today()
	if preset == "today":
		return (t, t)
	if preset == "this_week":
		start = t - timedelta(days=t.weekday())
		return (start, start + timedelta(days=6))
	if preset == "last_week":
		this_start = t - timedelta(days=t.weekday())
		end = this_start - timedelta(days=1)
		start = end - timedelta(days=6)
		return (start, end)
	if preset == "this_month":
		start = t.replace(day=1)
		nm = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
		return (start, nm - timedelta(days=1))
	if preset == "last_month":
		first_this = t.replace(day=1)
		end = first_this - timedelta(days=1)
		return (end.replace(day=1), end)
	if preset == "this_quarter":
		q = (t.month - 1) // 3 + 1
		start = date(t.year, 3 * (q - 1) + 1, 1)
		if q == 4:
			end = date(t.year, 12, 31)
		else:
			nq_first = date(t.year, 3 * q + 1, 1)
			end = nq_first - timedelta(days=1)
		return (start, end)
	if preset == "last_quarter":
		q = (t.month - 1) // 3 + 1
		if q == 1:
			start = date(t.year - 1, 10, 1)
			end = date(t.year - 1, 12, 31)
		else:
			start = date(t.year, 3 * (q - 2) + 1, 1)
			end = date(t.year, 3 * (q - 1) + 1, 1) - timedelta(days=1)
		return (start, end)
	if preset == "this_year":
		return (date(t.year, 1, 1), date(t.year, 12, 31))
	if preset == "last_year":
		return (date(t.year - 1, 1, 1), date(t.year - 1, 12, 31))
	if preset == "year_to_date":
		return (date(t.year, 1, 1), t)
	return None


def _detect_metric(low_prompt: str) -> tuple[str, str, str, str] | None:
	"""Return (source_doctype, metric_field, metric_fn, metric_label) or None."""
	for phrase, mapping in _METRIC_PHRASES:
		if re.search(rf"\b{re.escape(phrase)}\b", low_prompt):
			return mapping
	return None


def _detect_group_by(low_prompt: str) -> tuple[str, str] | None:
	"""Return (group_by_field, group_by_label) or None.

	Looks for either ``by <entity>`` or a ``top N <entity>s`` phrasing.
	The entity-word search is the richer signal since ``by X`` anywhere
	in the prompt is a strong group-by intent.
	"""
	# "by <entity>" wins when present - explicit GROUP BY cue.
	m = re.search(
		r"\bby\s+([a-z][a-z _-]{0,30}?)\b(?=\s|$|[,.;!?])",
		low_prompt,
	)
	if m:
		entity = m.group(1).strip()
		for phrase, mapping in _GROUP_BY_PHRASES:
			if phrase in entity or entity in phrase:
				return mapping
	# Fallback: ``top N <entity>s`` / bare entity plural in the prompt.
	for phrase, mapping in _GROUP_BY_PHRASES:
		if re.search(rf"\b{re.escape(phrase)}\b", low_prompt):
			return mapping
	return None


def _build_aggregation_sql(
	source_doctype: str,
	metric_field: str,
	metric_fn: str,
	metric_label: str,
	group_by_field: str,
	group_by_label: str,
	limit: int | None,
	date_range: tuple[date, date] | None,
) -> str:
	"""Render a Query Report SQL body for an aggregation query.

	TD-M7: date ranges use Frappe filter placeholders (``%(from_date)s``
	and ``%(to_date)s``) rather than literal dates. Frappe substitutes
	the current filter values at run time, so the same Report stays
	correct across quarters. The initial defaults come from
	``_build_aggregation_filters`` which pairs 1:1 with this SQL.

	Frappe Query Reports run the SQL as-is. We quote identifiers with
	backticks (MariaDB convention), filter to submitted rows
	(docstatus=1), group by the entity, aggregate the metric, order
	descending, and LIMIT.
	"""
	label_alias = metric_label.replace("`", "")
	group_alias = group_by_label.replace("`", "")
	parts = [
		"SELECT",
		f"    `{group_by_field}` AS `{group_alias}`,",
		f"    {metric_fn}(`{metric_field}`) AS `{label_alias}`",
		f"FROM `tab{source_doctype}`",
		"WHERE `docstatus` = 1",
	]
	if date_range is not None:
		# Filter placeholders — Frappe fills these at run time from the
		# filter UI; the default comes from the candidate's filters list.
		parts.append(
			"  AND `posting_date` BETWEEN %(from_date)s AND %(to_date)s"
		)
	parts.append(f"  AND `{group_by_field}` IS NOT NULL AND `{group_by_field}` != ''")
	parts.append(f"GROUP BY `{group_by_field}`")
	parts.append(f"ORDER BY `{label_alias}` DESC")
	if limit:
		parts.append(f"LIMIT {int(limit)}")
	sql = "\n".join(parts)

	# Belt-and-suspenders (TD-M8): validate the generated SQL against
	# the local safe-SELECT policy before handing it to the specialist
	# / pipeline safety net. Frappe's check_safe_sql_query is the
	# ultimate gate, but a bug in this builder shouldn't be able to
	# ship dangerous SQL into a Report document in the first place.
	from alfred.security.sql_safety import validate_safe_select
	validate_safe_select(sql)
	return sql


def _build_aggregation_filters(
	date_range: tuple[date, date] | None,
) -> list[dict]:
	"""Return the Frappe filter definitions that accompany the SQL from
	``_build_aggregation_sql``. Each filter's default lands in the
	Query Report's filter UI so the report runs correctly on first
	open AND the user can change the range to re-run for a different
	window. TD-M7.
	"""
	if date_range is None:
		return []
	return [
		{
			"fieldname": "from_date",
			"label": "From Date",
			"fieldtype": "Date",
			"default": date_range[0].isoformat(),
		},
		{
			"fieldname": "to_date",
			"label": "To Date",
			"fieldtype": "Date",
			"default": date_range[1].isoformat(),
		},
	]

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

# Markers that indicate the reply is schema narration, not actual data.
# When the agent falls back to lookup_doctype on a data-shaped prompt,
# the reply reads like documentation ("The Customer DocType has 83
# fields..."). A Save as Report button over schema would be misleading,
# so suppress it when any of these appear.
_SCHEMA_REPLY_MARKERS: tuple[str, ...] = (
	"defines the structure",
	"doctype represents",
	"doctype is organized",
	"permission grid",
	"permission rules",
	"the permissions define",
	"read-only fields",
	"field definitions",
	"83 fields",  # verbatim from the bug report; any "N fields" where agent narrates the schema
)

_COUNT_RE = re.compile(
	r"\b\d+\s+(?:\w+\s+)?"  # digit + optional single adjective ("42 pending invoices")
	r"(records?|rows?|entries?|results?|items?|products?|"
	r"customers?|invoices?|orders?|projects?|leads?|users?|employees?|documents?|"
	r"suppliers?|territor(?:y|ies)|sales\s+person(?:s)?|salespersons?)\b",
	re.IGNORECASE,
)
_MD_TABLE_ROW_RE = re.compile(r"^\s*\|.+\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
_NUMBERED_LIST_RE = re.compile(r"^\s*\d+[\.\)]\s+\S", re.MULTILINE)
_BULLET_LIST_RE = re.compile(r"^\s*[-\*]\s+\S", re.MULTILINE)


def _reply_looks_like_data(reply: str) -> bool:
	"""True iff the reply shows evidence of actual records.

	Heuristic combining three signals. Either is sufficient, and a
	schema-narration marker vetoes all of them - we don't want to
	offer Save as Report when the agent fell back to lookup_doctype
	and narrated the schema instead of calling get_list.
	"""
	if not reply:
		return False
	reply_low = reply.lower()
	for marker in _SCHEMA_REPLY_MARKERS:
		if marker in reply_low:
			return False

	# Markdown table with >= 2 body rows (past the header + separator).
	lines = reply.splitlines()
	body_rows = 0
	saw_sep = False
	for line in lines:
		if _MD_TABLE_SEP_RE.match(line):
			saw_sep = True
			continue
		if saw_sep and _MD_TABLE_ROW_RE.match(line):
			body_rows += 1
		elif saw_sep and not line.strip():
			saw_sep = False
	if body_rows >= 2:
		return True

	# Numbered or bulleted list with >= 3 items.
	if len(_NUMBERED_LIST_RE.findall(reply)) >= 3:
		return True
	if len(_BULLET_LIST_RE.findall(reply)) >= 3:
		return True

	# Explicit count phrase like "I found 42 customers".
	if _COUNT_RE.search(reply):
		return True

	return False


def extract_report_candidate(*, prompt: str, reply: str) -> ReportCandidate | None:
	"""Return a ReportCandidate when the prompt is report-shaped, else None.

	Two candidate shapes can come out:

	  1. ``Query Report`` with a ready-made SQL body - emitted when the
	     prompt names an aggregation metric ("revenue", "sales", "spend"),
	     a group-by entity ("customers", "suppliers", "territory"), and
	     optionally a top-N limit and time range. Report Builder cannot
	     express GROUP BY + SUM, so aggregation prompts must route to
	     Query Report.

	  2. ``Report Builder`` list-shape - the original V1 behaviour for
	     prompts like "customers this quarter" where the user wants a
	     filtered list, not an aggregation.

	Common gates apply to both:
	  - Reply must not be an error / empty-result message.
	  - Reply must look like actual data (rows, a list of records, or an
	    explicit count). Schema-narration replies are suppressed so the
	    Save as Report button stops appearing over lookup_doctype dumps.
	"""
	if not prompt:
		return None

	low = prompt.lower()
	reply_low = (reply or "").lower()

	for marker in _ERROR_REPLY_MARKERS:
		if marker in reply_low:
			return None

	if not _reply_looks_like_data(reply or ""):
		return None

	# Top-N limit and time range are shared between both shapes.
	limit: int | None = None
	m = _TOP_N_RE.search(low)
	if m:
		limit = int(m.group(1))

	time_range: dict | None = None
	date_range: tuple[date, date] | None = None
	for phrase, preset in _TIME_RANGE_PRESETS:
		if phrase in low:
			time_range = {"field": "posting_date", "preset": preset}
			date_range = _resolve_preset_range(preset)
			break

	# ── Shape 1: Query Report (aggregation) ──────────────────────
	metric = _detect_metric(low)
	group_by = _detect_group_by(low)
	if metric is not None and group_by is not None:
		source_doctype, metric_field, metric_fn, metric_label = metric
		group_by_field, group_by_label = group_by
		query = _build_aggregation_sql(
			source_doctype=source_doctype,
			metric_field=metric_field,
			metric_fn=metric_fn,
			metric_label=metric_label,
			group_by_field=group_by_field,
			group_by_label=group_by_label,
			limit=limit,
			date_range=date_range,
		)
		filters = _build_aggregation_filters(date_range)
		name_parts: list[str] = []
		if limit:
			name_parts.append(f"Top {limit}")
		name_parts.append(group_by_label + ("s" if not group_by_label.endswith("s") else ""))
		name_parts.append(f"by {metric_label}")
		if time_range:
			preset_h = time_range["preset"].replace("_", " ").title()
			name_parts.append(f"- {preset_h}")
		suggested_name = " ".join(name_parts)

		return ReportCandidate(
			target_doctype=source_doctype,
			report_type="Query Report",
			filters=filters,
			limit=limit,
			time_range=time_range,
			suggested_name=suggested_name,
			query=query,
			aggregation={
				"source_doctype": source_doctype,
				"metric_field": metric_field,
				"metric_fn": metric_fn,
				"metric_label": metric_label,
				"group_by_field": group_by_field,
				"group_by_label": group_by_label,
			},
		)

	# ── Shape 2: Report Builder list (original V1 behaviour) ─────
	target = _detect_target_doctype(low)
	if target is None:
		return None

	# Report-shape signal: need at least a limit or a time range. A prompt
	# like "what's customer X's credit limit" has a DocType (Customer) but
	# is not report-shaped - it's a scalar lookup.
	if limit is None and time_range is None:
		return None

	# Suggested name: "Top 10 Customers - This Quarter" style
	name_parts = []
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
