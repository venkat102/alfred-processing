"""Tests for the heuristic report_candidate extractor.

Doesn't exercise the full Insights handler (that requires an MCP / LLM
stack). Tests the pure extraction function in isolation.
"""

from alfred.handlers.insights_candidate import extract_report_candidate


def test_top_n_customers_this_quarter_produces_candidate():
	c = extract_report_candidate(
		prompt="Show top 10 customers by revenue this quarter",
		reply="Here are your top 10 customers: ...",
	)
	assert c is not None
	assert c.target_doctype == "Customer"
	assert c.limit == 10
	assert c.time_range == {"field": "posting_date", "preset": "this_quarter"}
	assert c.suggested_name == "Top 10 Customers - This Quarter"


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
