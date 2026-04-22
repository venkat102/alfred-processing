"""Regression: drift detector must not flag valid JSON-array output.

The Report Builder specialist emits `"report_type": "Report Builder"` and
rationale strings mentioning "Query Report" / "Script Report". Before the
short-circuit these matched the Title-Cased DocType regex and triggered
false-positive drift. Any JSON-array output now short-circuits regardless
of which specialist produced it.
"""

import json

from alfred.api.pipeline import _detect_drift


def test_valid_report_changeset_does_not_drift():
	changeset = [{
		"op": "create",
		"doctype": "Report",
		"data": {
			"doctype": "Report",
			"name": "Sales Performance Summary",
			"ref_doctype": "Sales Order",
			"report_type": "Report Builder",
			"is_standard": 0,
			"module": "Selling",
		},
		"field_defaults_meta": {
			"report_type": {
				"source": "default",
				"rationale": (
					"Report Builder is the safest default: field-list + filters, "
					"no SQL or Python. Promote to Query Report only when "
					"aggregations require raw SQL; Script Report is V2+ only."
				),
			},
			"ref_doctype": {"source": "user"},
		},
	}]
	result_text = json.dumps(changeset, indent=2)
	assert _detect_drift(result_text, user_prompt="Create a sales performance report") is None


def test_valid_doctype_changeset_does_not_drift():
	changeset = [{
		"op": "create",
		"doctype": "DocType",
		"data": {
			"doctype": "DocType",
			"name": "Book",
			"module": "Custom",
			"autoname": "autoincrement",
		},
	}]
	result_text = json.dumps(changeset)
	assert _detect_drift(result_text, user_prompt="Create a DocType called Book") is None


def test_json_array_wrapped_in_code_fence_does_not_drift():
	body = json.dumps([{
		"op": "create", "doctype": "Report",
		"data": {"report_type": "Report Builder", "name": "Top Customers"},
	}])
	result_text = f"```json\n{body}\n```"
	assert _detect_drift(result_text, user_prompt="save as report") is None


def test_prose_with_foreign_doctypes_still_drifts():
	# Long prose mentioning multiple unrelated DocTypes - real drift
	text = (
		"Sure, here is how Sales Invoice works with Customer and Supplier. "
		"You'll also want to understand Journal Entry and Payment Entry. "
		"The Purchase Order and Delivery Note are also important. "
		+ "This documentation explains the typical flow. " * 30
	)
	assert _detect_drift(text, user_prompt="Create a DocType called Book") is not None


def test_long_prose_without_json_still_drifts():
	text = "documentation: " + ("this is a long prose dump. " * 200)
	assert _detect_drift(text, user_prompt="Create a DocType") is not None


def test_malformed_json_array_falls_through_to_drift_checks():
	# Starts with [ but not parseable - still eligible for drift checks
	# so the usual rescue path can fire.
	text = "[ broken json with lots of Sales Invoice and Journal Entry prose" + " words" * 200
	# Could still be flagged as drift or could pass - point is no exception.
	result = _detect_drift(text, user_prompt="Create a DocType")
	assert result is None or isinstance(result, str)
