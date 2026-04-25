"""Tests for alfred.security.sql_safety.validate_safe_select (TD-M8)."""

from __future__ import annotations

import pytest

from alfred.security.sql_safety import UnsafeSqlError, validate_safe_select

# ── Valid SELECTs (must not raise) ─────────────────────────────────


def test_simple_select_ok():
	validate_safe_select("SELECT 1")


def test_select_with_from_and_where_ok():
	validate_safe_select(
		"SELECT name, grand_total FROM `tabSales Invoice` WHERE docstatus = 1"
	)


def test_select_with_group_by_and_aggregates_ok():
	validate_safe_select("""
		SELECT customer, SUM(grand_total) AS revenue
		FROM `tabSales Invoice`
		WHERE docstatus = 1
		GROUP BY customer
		ORDER BY revenue DESC
		LIMIT 10
	""")


def test_select_with_join_ok():
	validate_safe_select("""
		SELECT si.name, c.customer_name
		FROM `tabSales Invoice` si
		JOIN `tabCustomer` c ON c.name = si.customer
	""")


def test_select_with_cte_ok():
	validate_safe_select("""
		WITH monthly AS (
			SELECT MONTH(posting_date) AS m, SUM(grand_total) AS total
			FROM `tabSales Invoice`
			GROUP BY m
		)
		SELECT * FROM monthly ORDER BY total DESC
	""")


def test_select_with_trailing_semicolon_ok():
	validate_safe_select("SELECT 1;")


def test_select_with_leading_block_comment_ok():
	validate_safe_select("/* report: top customers */ SELECT 1")


def test_select_with_inline_line_comment_ok():
	validate_safe_select(
		"SELECT name -- the report name\nFROM `tabReport`"
	)


def test_select_where_keyword_appears_in_column_name_ok():
	# Column names like `update_date` contain 'UPDATE' — must NOT trip
	# the dangerous-keyword scan (that would be a false positive).
	validate_safe_select(
		"SELECT update_date, drop_off FROM `tabFoo` WHERE delete_flag = 0"
	)


def test_string_literal_containing_dangerous_word_ok():
	# 'DROP TABLE x' appears in a string literal — must NOT raise.
	validate_safe_select(
		"SELECT name FROM `tabReport` WHERE title = 'DROP TABLE x'"
	)


def test_commented_out_dangerous_keyword_ok():
	validate_safe_select(
		"SELECT 1 -- DROP TABLE x\n"
	)


# ── Reject: non-SELECT statements ──────────────────────────────────


def test_empty_sql_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("")
	assert exc.value.reason == "empty"


def test_none_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select(None)  # type: ignore[arg-type]
	assert exc.value.reason == "empty"


def test_whitespace_only_rejected():
	with pytest.raises(UnsafeSqlError):
		validate_safe_select("   \n  \n  ")


def test_only_comments_rejected():
	with pytest.raises(UnsafeSqlError):
		validate_safe_select("/* nothing */ -- nothing\n")


def test_insert_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("INSERT INTO `tabFoo` (name) VALUES ('x')")
	assert exc.value.reason == "dangerous_keyword"


def test_update_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("UPDATE `tabFoo` SET name = 'x'")
	assert exc.value.reason == "dangerous_keyword"


def test_delete_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("DELETE FROM `tabFoo`")
	assert exc.value.reason == "dangerous_keyword"


def test_drop_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("DROP TABLE `tabFoo`")
	assert exc.value.reason == "dangerous_keyword"


def test_create_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("CREATE TABLE foo (x INT)")
	assert exc.value.reason == "dangerous_keyword"


def test_alter_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("ALTER TABLE foo ADD COLUMN y INT")
	assert exc.value.reason == "dangerous_keyword"


def test_truncate_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("TRUNCATE TABLE foo")
	assert exc.value.reason == "dangerous_keyword"


def test_grant_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("GRANT ALL ON foo TO user")
	assert exc.value.reason == "dangerous_keyword"


def test_call_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("CALL some_procedure()")
	assert exc.value.reason == "dangerous_keyword"


# ── Reject: multi-statement ────────────────────────────────────────


def test_select_then_drop_rejected():
	# Classic SQL-injection tail.
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("SELECT 1; DROP TABLE x")
	# The multi-statement check fires first since ';' is counted
	# before keyword scan; either reason is fine — we're blocking
	# the right thing.
	assert exc.value.reason in ("multi_statement", "dangerous_keyword")


def test_double_select_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("SELECT 1; SELECT 2")
	assert exc.value.reason == "multi_statement"


def test_select_then_update_rejected():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("SELECT 1; UPDATE x SET y = 1")
	assert exc.value.reason in ("multi_statement", "dangerous_keyword")


# ── Reject: wrong leading statement ────────────────────────────────


def test_show_rejected_not_select():
	# SHOW TABLES doesn't include dangerous keywords but still isn't
	# SELECT — out of policy for Query Reports.
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("SHOW TABLES")
	assert exc.value.reason == "not_select"


def test_describe_rejected_not_select():
	with pytest.raises(UnsafeSqlError) as exc:
		validate_safe_select("DESCRIBE foo")
	assert exc.value.reason == "not_select"


# ── Integration with the aggregation SQL builder ──────────────────


def test_aggregation_sql_passes_validator():
	# The SQL produced by _build_aggregation_sql must always pass —
	# this test catches any regression that would make the builder
	# emit bad SQL. Circular but useful belt-and-suspenders.
	from datetime import date

	from alfred.handlers.insights_candidate import _build_aggregation_sql

	sql = _build_aggregation_sql(
		source_doctype="Sales Invoice",
		metric_field="grand_total",
		metric_fn="SUM",
		metric_label="Revenue",
		group_by_field="customer",
		group_by_label="Customer",
		limit=10,
		date_range=(date(2026, 4, 1), date(2026, 6, 30)),
	)
	# Already called internally, but re-validate to make the
	# coupling explicit.
	validate_safe_select(sql)
