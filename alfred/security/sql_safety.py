"""Local safe-SQL validator — belt-and-suspenders for Frappe's runtime
check. TD-M8.

When Alfred generates Query Report SQL for the Insights-to-Report
handoff (alfred/handlers/insights_candidate.py::_build_aggregation_sql),
the SQL lands in Frappe's ``Report.query`` field. Frappe's own
``check_safe_sql_query()`` runs at execution time and rejects DDL /
DML / multi-statement. That check is our ultimate defence — but if
it has a gap (regex miss, new SQL syntax, whitespace-bypass), a bad
SQL string reaches the database.

This module is the OUTER defence: we reject dangerous shapes before
Frappe ever sees them. Goal is conservative — false-positive on some
exotic valid SELECTs is acceptable; false-negative on any write
operation is not.

What we reject:
  - Multi-statement strings (``SELECT 1; DROP TABLE x``)
  - DDL/DML keywords appearing as a whole-token outside string
    literals (``INSERT``, ``UPDATE``, ``DELETE``, ``DROP``, ``CREATE``,
    ``ALTER``, ``TRUNCATE``, ``GRANT``, ``REVOKE``, ``REPLACE``,
    ``EXEC``, ``CALL``, ``LOAD``, ``HANDLER``, ``MERGE``,
    ``SET``, ``LOCK``, ``UNLOCK``)
  - Anything that doesn't start with ``SELECT`` (after stripping
    whitespace + leading ``/* comment */`` blocks)

What we allow:
  - Any well-formed single SELECT statement, including joins,
    aggregates, CTEs, subqueries.
  - Leading comments (``/* … */``) — common in query-report metadata.
  - Inline ``--`` or ``#`` comments mid-statement.
"""

from __future__ import annotations

import re


class UnsafeSqlError(ValueError):
	"""Raised when a SQL string fails the safe-SELECT policy.

	Carries a ``reason`` attribute matching the rejection category.
	"""

	def __init__(self, message: str, reason: str):
		super().__init__(message)
		self.reason = reason


# Keywords that signal data-mutation or privileged ops. Matched as
# whole words (``\b``) so ``SELECT update_date FROM x`` is fine but
# ``UPDATE x SET …`` is not.
_DANGEROUS_KEYWORDS: tuple[str, ...] = (
	"INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
	"TRUNCATE", "GRANT", "REVOKE", "REPLACE", "EXEC", "CALL",
	"LOAD", "HANDLER", "MERGE", "SET", "LOCK", "UNLOCK",
)

_DANGEROUS_RE = re.compile(
	r"\b(?:" + "|".join(_DANGEROUS_KEYWORDS) + r")\b",
	re.IGNORECASE,
)

# Strip leading comments: `/* ... */` and full `-- ...\n` / `# ...\n`
# lines at the head of the query.
_LEADING_COMMENT_RE = re.compile(
	r"^\s*(?:/\*[\s\S]*?\*/|--[^\n]*\n|\#[^\n]*\n)",
)


def _strip_leading_comments(sql: str) -> str:
	"""Remove any leading /*…*/, ``-- …``, or ``# …`` comments."""
	prev = None
	out = sql
	while out != prev:
		prev = out
		out = _LEADING_COMMENT_RE.sub("", out, count=1)
	return out.lstrip()


def _strip_string_literals(sql: str) -> str:
	"""Replace every '…' or "…" string literal with an empty quote
	pair. This is conservative: we don't want a literal containing
	``DROP`` to trigger the dangerous-keyword check. Not a SQL parser
	— good enough for the reject policy.
	"""
	# Handle escaped quotes within strings (SQL style: '' or \\').
	single = re.compile(r"'(?:''|\\.|[^'\\])*'", re.DOTALL)
	double = re.compile(r'"(?:""|\\.|[^"\\])*"', re.DOTALL)
	return double.sub('""', single.sub("''", sql))


def _strip_inline_comments(sql: str) -> str:
	"""Remove ``-- …`` and ``# …`` line comments, plus ``/* … */`` blocks.

	These MUST go before dangerous-keyword scan so a commented-out
	``-- DROP TABLE x`` doesn't falsely reject.
	"""
	# Block comments first (non-greedy).
	sql = re.sub(r"/\*[\s\S]*?\*/", " ", sql)
	# Line comments to end-of-line.
	sql = re.sub(r"(?:--|\#)[^\n]*", " ", sql)
	return sql


def _count_statements(sql: str) -> int:
	"""Count SQL statements separated by ``;`` outside string literals.

	Input should already have literals stripped. Trailing ``;`` is
	ignored (common DB-client idiom: ``SELECT 1;``).
	"""
	trimmed = sql.rstrip().rstrip(";").strip()
	if not trimmed:
		return 0
	# Every remaining ``;`` marks a boundary between statements.
	return 1 + trimmed.count(";")


def validate_safe_select(sql: str) -> None:
	"""Validate that ``sql`` is a single safe SELECT statement.

	Raises ``UnsafeSqlError`` (a ValueError subclass) on reject.

	Args:
		sql: The raw SQL to validate.

	Raises:
		UnsafeSqlError with ``reason`` in {empty, multi_statement,
			dangerous_keyword, not_select}.
	"""
	if not sql or not isinstance(sql, str):
		raise UnsafeSqlError("SQL is empty or not a string", reason="empty")

	# Strip comments + literals so the dangerous-keyword scan looks
	# only at the actual statement structure.
	sanitised = _strip_inline_comments(sql)
	sanitised = _strip_string_literals(sanitised)

	# Multi-statement check.
	n = _count_statements(sanitised)
	if n == 0:
		raise UnsafeSqlError("SQL is empty after stripping comments", reason="empty")
	if n > 1:
		raise UnsafeSqlError(
			f"SQL contains {n} statements; only single-SELECT is allowed",
			reason="multi_statement",
		)

	# Dangerous-keyword scan.
	m = _DANGEROUS_RE.search(sanitised)
	if m:
		raise UnsafeSqlError(
			f"SQL contains disallowed keyword {m.group(0)!r}; "
			"only SELECT is permitted",
			reason="dangerous_keyword",
		)

	# First non-comment token must be SELECT (or WITH, for CTEs).
	first_token_match = re.match(r"\s*([a-zA-Z_]+)", _strip_leading_comments(sanitised))
	if not first_token_match:
		raise UnsafeSqlError(
			"SQL does not start with a SELECT or WITH statement",
			reason="not_select",
		)
	first_token = first_token_match.group(1).upper()
	if first_token not in ("SELECT", "WITH"):
		raise UnsafeSqlError(
			f"SQL starts with {first_token!r}; only SELECT and WITH are allowed",
			reason="not_select",
		)
