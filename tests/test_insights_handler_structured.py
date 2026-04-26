"""Tests for the heuristic report_candidate extractor.

Doesn't exercise the full Insights handler (that requires an MCP / LLM
stack). Tests the pure extraction function in isolation.
"""

from datetime import date

from alfred.handlers.insights_candidate import (
	_build_aggregation_sql,
	_resolve_preset_range,
	extract_report_candidate,
)


def test_top_n_customers_by_revenue_produces_query_report():
	# Regression: the previous V1 extractor picked target_doctype=Customer
	# and report_type=Report Builder for this prompt. Customer has no
	# revenue column and Report Builder can't express GROUP BY + SUM,
	# so the generated report shipped a flat Sales Invoice list instead
	# of aggregated revenue-per-customer. The aggregation branch fixes
	# the ref_doctype (Sales Invoice), the report type (Query Report),
	# and emits a ready-made SQL body.
	c = extract_report_candidate(
		prompt="Show top 10 customers by revenue this quarter",
		reply="Here are your top 10 customers: ...",
	)
	assert c is not None
	assert c.target_doctype == "Sales Invoice"
	assert c.report_type == "Query Report"
	assert c.limit == 10
	assert c.time_range == {"field": "posting_date", "preset": "this_quarter"}
	assert c.aggregation is not None
	assert c.aggregation["source_doctype"] == "Sales Invoice"
	assert c.aggregation["metric_field"] == "grand_total"
	assert c.aggregation["metric_fn"] == "SUM"
	assert c.aggregation["group_by_field"] == "customer"
	assert "Top 10 Customers by Revenue - This Quarter" == c.suggested_name
	assert c.query is not None
	assert "GROUP BY" in c.query.upper()
	assert "SUM(" in c.query.upper()
	assert "LIMIT 10" in c.query.upper()


def test_scalar_answer_returns_none():
	# No top-N, no time range, just a single-value lookup
	c = extract_report_candidate(
		prompt="What is customer ACME's credit limit?",
		reply="Customer ACME has a credit limit of 50000.",
	)
	assert c is None


def test_no_target_doctype_returns_none():
	c = extract_report_candidate(
		prompt="top 10 things this quarter",
		reply="Here are some things.",
	)
	assert c is None


def test_error_reply_returns_none():
	c = extract_report_candidate(
		prompt="Show top 5 customers this year",
		reply="I couldn't find any customers matching those filters.",
	)
	assert c is None


def test_time_range_only_produces_candidate():
	# Reply is a numbered list so _reply_looks_like_data accepts it as
	# actual data (vs schema narration or a stub reply).
	c = extract_report_candidate(
		prompt="list customers from last month",
		reply=(
			"Here are the customers from last month:\n"
			"1. ACME Corp\n"
			"2. Globex Inc\n"
			"3. Initech Ltd\n"
		),
	)
	assert c is not None
	assert c.target_doctype == "Customer"
	assert c.limit is None
	assert c.time_range["preset"] == "last_month"


def test_top_n_only_produces_candidate():
	c = extract_report_candidate(
		prompt="list the top 5 suppliers",
		reply=(
			"Here are the top 5 suppliers by spend:\n"
			"1. Alpha Supplies\n"
			"2. Beta Traders\n"
			"3. Gamma Imports\n"
			"4. Delta Goods\n"
			"5. Epsilon Ltd\n"
		),
	)
	assert c is not None
	assert c.target_doctype == "Supplier"
	assert c.limit == 5
	assert c.time_range is None


def test_prompt_with_no_report_shape_returns_none():
	# Has a DocType mention but no limit, no time range -> not report-shaped
	c = extract_report_candidate(
		prompt="tell me about the Customer doctype",
		reply="The Customer DocType has fields for name, ...",
	)
	assert c is None


def test_ytd_variant_matches():
	c = extract_report_candidate(
		prompt="Show top 5 customers YTD",
		reply="Top 5 customers YTD: ...",
	)
	assert c is not None
	assert c.time_range["preset"] == "year_to_date"


def test_plural_doctype_fallback():
	c = extract_report_candidate(
		prompt="Show top 3 employees this year",
		reply="Top 3 employees this year: ...",
	)
	assert c is not None
	assert c.target_doctype == "Employee"


def test_suggested_name_without_time_range():
	c = extract_report_candidate(
		prompt="Show top 7 suppliers",
		reply=(
			"Top 7 suppliers:\n"
			"1. Alpha\n"
			"2. Beta\n"
			"3. Gamma\n"
			"4. Delta\n"
			"5. Epsilon\n"
			"6. Zeta\n"
			"7. Eta\n"
		),
	)
	assert c is not None
	assert c.suggested_name == "Top 7 Suppliers"


# ── Aggregation branch (Query Report) ──────────────────────────────


def test_aggregation_spend_by_supplier_last_quarter():
	c = extract_report_candidate(
		prompt="top 5 suppliers by spend last quarter",
		reply="Here are your top 5 suppliers by spend: ...",
	)
	assert c is not None
	assert c.report_type == "Query Report"
	assert c.target_doctype == "Purchase Invoice"
	assert c.aggregation["source_doctype"] == "Purchase Invoice"
	assert c.aggregation["group_by_field"] == "supplier"
	assert c.aggregation["metric_label"] == "Spend"
	assert c.limit == 5
	assert "LIMIT 5" in c.query.upper()
	assert "`supplier`" in c.query
	assert "`tabPurchase Invoice`" in c.query


def test_aggregation_sales_by_territory_no_limit():
	c = extract_report_candidate(
		prompt="sales by territory this year",
		reply=(
			"Here are sales by territory this year:\n"
			"1. North: 1.2M\n"
			"2. South: 800K\n"
			"3. East: 650K\n"
		),
	)
	assert c is not None
	assert c.report_type == "Query Report"
	assert c.target_doctype == "Sales Invoice"
	assert c.aggregation["group_by_field"] == "territory"
	# No top-N -> no LIMIT clause
	assert "LIMIT" not in c.query.upper()


def test_aggregation_without_metric_falls_back_to_report_builder():
	# "top 10 customers this quarter" has a group-by entity and a
	# time range but no metric phrase - falls back to the simple
	# Report Builder list shape.
	c = extract_report_candidate(
		prompt="Show top 10 customers this quarter",
		reply="Here are your top 10 customers: ...",
	)
	assert c is not None
	assert c.report_type == "Report Builder"
	assert c.target_doctype == "Customer"
	assert c.query is None
	assert c.aggregation is None


def test_aggregation_without_group_by_falls_back():
	# Metric without a group-by entity - e.g. "total revenue this quarter"
	# is a scalar, not an aggregation shape we can render as a list.
	c = extract_report_candidate(
		prompt="total revenue this quarter",
		reply="Total revenue this quarter is 1.2M.",
	)
	# Either None (no group-by entity + no top-N) or a non-aggregation
	# candidate - just assert we did not emit a Query Report.
	if c is not None:
		assert c.report_type != "Query Report"
		assert c.query is None


def test_aggregation_sql_well_formed():
	c = extract_report_candidate(
		prompt="top 3 customers by revenue this month",
		reply="Here are your top 3 customers: ...",
	)
	assert c is not None
	sql = c.query
	# Every identifier that gets interpolated lands inside backticks;
	# no stray curly braces (prompt-interpolation safety).
	assert "{" not in sql and "}" not in sql
	assert "SELECT" in sql.upper()
	assert "FROM `tabSales Invoice`" in sql
	assert "`docstatus` = 1" in sql
	# TD-M7: date range uses Frappe filter placeholders, not literals.
	assert "%(from_date)s" in sql
	assert "%(to_date)s" in sql
	assert "GROUP BY `customer`" in sql
	assert "ORDER BY `Revenue` DESC" in sql
	assert "LIMIT 3" in sql
	# Filter definitions accompany the SQL so Frappe fills defaults.
	assert c.filters is not None
	assert any(f["fieldname"] == "from_date" for f in c.filters)
	assert any(f["fieldname"] == "to_date" for f in c.filters)


# ── _resolve_preset_range ──────────────────────────────────────────


def test_preset_this_quarter_q2_spans_apr_to_jun():
	start, end = _resolve_preset_range("this_quarter", today=date(2026, 4, 24))
	assert start == date(2026, 4, 1)
	assert end == date(2026, 6, 30)


def test_preset_last_quarter_from_q1_wraps_to_prev_year():
	start, end = _resolve_preset_range("last_quarter", today=date(2026, 2, 15))
	assert start == date(2025, 10, 1)
	assert end == date(2025, 12, 31)


def test_preset_this_year():
	start, end = _resolve_preset_range("this_year", today=date(2026, 4, 24))
	assert start == date(2026, 1, 1)
	assert end == date(2026, 12, 31)


def test_preset_year_to_date_ends_on_today():
	start, end = _resolve_preset_range("year_to_date", today=date(2026, 4, 24))
	assert start == date(2026, 1, 1)
	assert end == date(2026, 4, 24)


def test_preset_unknown_returns_none():
	assert _resolve_preset_range("next_millennium") is None


# ── _build_aggregation_sql ─────────────────────────────────────────


def test_build_sql_without_date_range_omits_between_clause():
	sql = _build_aggregation_sql(
		source_doctype="Sales Invoice",
		metric_field="grand_total",
		metric_fn="SUM",
		metric_label="Revenue",
		group_by_field="customer",
		group_by_label="Customer",
		limit=10,
		date_range=None,
	)
	assert "BETWEEN" not in sql.upper()
	assert "LIMIT 10" in sql.upper()
