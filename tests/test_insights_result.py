import pytest
from pydantic import ValidationError

from alfred.models.insights_result import InsightsResult, ReportCandidate


def test_report_candidate_minimal():
	c = ReportCandidate(target_doctype="Customer")
	assert c.target_doctype == "Customer"
	assert c.report_type == "Report Builder"
	assert c.columns == []
	assert c.limit is None


def test_report_candidate_target_doctype_required():
	with pytest.raises(ValidationError):
		ReportCandidate()


def test_report_candidate_to_handoff_prompt_minimal():
	c = ReportCandidate(target_doctype="Customer")
	out = c.to_handoff_prompt()
	assert "Save as Report:" in out
	assert "Source DocType: Customer" in out
	assert "Report type: Report Builder" in out


def test_report_candidate_to_handoff_prompt_full():
	c = ReportCandidate(
		target_doctype="Customer",
		columns=[
			{"fieldname": "customer_name", "label": "Customer"},
			{"fieldname": "customer_group", "label": "Group"},
		],
		filters=[{"fieldname": "status", "operator": "=", "value": "Active"}],
		sort=[{"fieldname": "revenue", "direction": "desc"}],
		limit=10,
		time_range={"field": "posting_date", "preset": "this_quarter"},
		suggested_name="Top 10 Customers - This Quarter",
	)
	out = c.to_handoff_prompt()
	assert "Top 10 Customers - This Quarter" in out
	assert "Customer, Group" in out
	assert "status = Active" in out
	assert "revenue DESC" in out
	assert "Limit: 10" in out
	assert "this_quarter" in out


def test_insights_result_reply_only():
	r = InsightsResult(reply="hello")
	assert r.reply == "hello"
	assert r.report_candidate is None


def test_insights_result_with_candidate():
	r = InsightsResult(
		reply="your top 10 ...",
		report_candidate=ReportCandidate(target_doctype="Customer", limit=10),
	)
	assert r.reply == "your top 10 ..."
	assert r.report_candidate is not None
	assert r.report_candidate.target_doctype == "Customer"


def test_insights_result_serializes_cleanly():
	r = InsightsResult(
		reply="x",
		report_candidate=ReportCandidate(target_doctype="Customer", limit=5),
	)
	dumped = r.model_dump()
	assert dumped["reply"] == "x"
	assert dumped["report_candidate"]["target_doctype"] == "Customer"
	assert dumped["report_candidate"]["limit"] == 5
	restored = InsightsResult.model_validate(dumped)
	assert restored.report_candidate.limit == 5


def test_report_candidate_to_handoff_omits_empty_fields():
	c = ReportCandidate(target_doctype="Customer")
	out = c.to_handoff_prompt()
	# No columns / filters / sort / limit / time_range lines when absent
	assert "Columns:" not in out
	assert "Filters:" not in out
	assert "Sort:" not in out
	assert "Limit:" not in out
	assert "Time range:" not in out
