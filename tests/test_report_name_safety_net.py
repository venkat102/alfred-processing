"""V4 safety net: Report changesets get report_name from the handoff
candidate's suggested_name when the specialist's output omitted it.
"""

from unittest.mock import MagicMock

from alfred.api.pipeline import PipelineContext


def _ctx_with_report_candidate(suggested_name):
	conn = MagicMock()
	conn.site_config = {}
	c = PipelineContext(conn=conn, conversation_id="t", prompt="p")
	c.mode = "dev"
	c.intent = "create_report"
	c.report_candidate = {
		"target_doctype": "Customer",
		"report_type": "Report Builder",
		"suggested_name": suggested_name,
	} if suggested_name is not None else None
	return c


def _apply_safety_net(ctx):
	"""Thin adapter calling the real production function (TD-H9).

	Before TD-H1/H9 this helper was a COPY of the production block —
	test/prod drift waiting to happen. Now it delegates to the real
	``apply_report_handoff_safety_net`` so a behavioural change in
	production automatically flows through these tests.
	"""
	from alfred.api.safety_nets import apply_report_handoff_safety_net
	apply_report_handoff_safety_net(ctx)


def test_fills_report_name_when_missing():
	ctx = _ctx_with_report_candidate("Top 10 Customers - This Quarter")
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {"ref_doctype": "Customer", "report_type": "Report Builder"},
	}]
	_apply_safety_net(ctx)
	assert ctx.changes[0]["data"]["report_name"] == "Top 10 Customers - This Quarter"
	assert ctx.changes[0]["field_defaults_meta"]["report_name"]["source"] == "default"


def test_does_not_overwrite_existing_report_name():
	ctx = _ctx_with_report_candidate("Fallback Name")
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {"report_name": "LLM Chose This", "ref_doctype": "Customer"},
	}]
	_apply_safety_net(ctx)
	assert ctx.changes[0]["data"]["report_name"] == "LLM Chose This"


def test_noop_when_no_candidate():
	ctx = _ctx_with_report_candidate(None)
	ctx.changes = [{"op": "create", "doctype": "Report", "data": {}}]
	_apply_safety_net(ctx)
	assert ctx.changes[0]["data"].get("report_name") is None


def test_noop_when_candidate_has_no_suggested_name():
	ctx = _ctx_with_report_candidate("")
	ctx.changes = [{"op": "create", "doctype": "Report", "data": {}}]
	_apply_safety_net(ctx)
	assert ctx.changes[0]["data"].get("report_name") is None


def test_noop_for_non_report_items():
	ctx = _ctx_with_report_candidate("X")
	ctx.changes = [{
		"op": "create", "doctype": "Server Script",
		"data": {"script_type": "DocType Event"},
	}]
	_apply_safety_net(ctx)
	assert "report_name" not in ctx.changes[0]["data"]


def test_noop_when_intent_is_not_create_report():
	ctx = _ctx_with_report_candidate("X")
	ctx.intent = "create_doctype"
	ctx.changes = [{"op": "create", "doctype": "Report", "data": {}}]
	_apply_safety_net(ctx)
	assert "report_name" not in ctx.changes[0]["data"]


def test_fills_only_empty_string_not_populated():
	ctx = _ctx_with_report_candidate("Handoff Name")
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {"report_name": ""},
	}]
	_apply_safety_net(ctx)
	assert ctx.changes[0]["data"]["report_name"] == "Handoff Name"


# ── Aggregation safety net ─────────────────────────────────────────


def _ctx_with_aggregation_candidate(query: str = "SELECT 1"):
	conn = MagicMock()
	conn.site_config = {}
	c = PipelineContext(conn=conn, conversation_id="t", prompt="p")
	c.mode = "dev"
	c.intent = "create_report"
	c.report_candidate = {
		"target_doctype": "Sales Invoice",
		"report_type": "Query Report",
		"suggested_name": "Top 10 Customers by Revenue - This Quarter",
		"query": query,
		"aggregation": {
			"source_doctype": "Sales Invoice",
			"metric_field": "grand_total",
			"metric_fn": "SUM",
			"metric_label": "Revenue",
			"group_by_field": "customer",
			"group_by_label": "Customer",
		},
	}
	return c


def _apply_aggregation_safety_net(ctx):
	"""Thin adapter calling the real production function (TD-H9).

	Before TD-H1/H9 this helper was a copy of the production block; now
	it delegates to ``apply_report_handoff_safety_net`` which handles
	both report_name AND aggregation in one pass (the production path
	always did; the test was the outlier).
	"""
	from alfred.api.safety_nets import apply_report_handoff_safety_net
	apply_report_handoff_safety_net(ctx)


def test_aggregation_overwrites_report_type_from_report_builder():
	# Specialist emitted Report Builder; safety net MUST force Query
	# Report because Report Builder can't do GROUP BY + SUM.
	ctx = _ctx_with_aggregation_candidate(query="SELECT customer, SUM(grand_total) FROM ...")
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {
			"report_name": "X",
			"report_type": "Report Builder",  # specialist got it wrong
			"ref_doctype": "Customer",         # specialist got it wrong
		},
	}]
	_apply_aggregation_safety_net(ctx)
	assert ctx.changes[0]["data"]["report_type"] == "Query Report"
	assert ctx.changes[0]["data"]["ref_doctype"] == "Sales Invoice"
	assert ctx.changes[0]["data"]["query"] == "SELECT customer, SUM(grand_total) FROM ..."


def test_aggregation_overwrites_specialist_query_when_different():
	# Specialist may emit a non-aggregation query; handoff's SQL is
	# authoritative. Override even when specialist already populated it.
	ctx = _ctx_with_aggregation_candidate(query="SELECT customer, SUM(grand_total) ...")
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {
			"report_type": "Query Report",
			"query": "SELECT * FROM tabSalesInvoice",  # wrong SQL from specialist
		},
	}]
	_apply_aggregation_safety_net(ctx)
	assert ctx.changes[0]["data"]["query"] == "SELECT customer, SUM(grand_total) ..."


def test_aggregation_fills_is_standard_default():
	ctx = _ctx_with_aggregation_candidate()
	ctx.changes = [{
		"op": "create", "doctype": "Report",
		"data": {"report_type": "Query Report", "query": "SELECT 1"},
	}]
	_apply_aggregation_safety_net(ctx)
	assert ctx.changes[0]["data"]["is_standard"] == "No"


def test_aggregation_safety_net_noop_without_aggregation_block():
	# Candidate has no aggregation/query -> don't touch report_type.
	# This is the Report Builder list-shape path.
	conn = MagicMock()
	conn.site_config = {}
	c = PipelineContext(conn=conn, conversation_id="t", prompt="p")
	c.mode = "dev"
	c.intent = "create_report"
	c.report_candidate = {
		"target_doctype": "Customer",
		"report_type": "Report Builder",
		"suggested_name": "Top 10 Customers - This Quarter",
	}
	c.changes = [{
		"op": "create", "doctype": "Report",
		"data": {"report_type": "Report Builder", "ref_doctype": "Customer"},
	}]
	_apply_aggregation_safety_net(c)
	# Nothing forced - Report Builder list shape is the intended output.
	assert c.changes[0]["data"]["report_type"] == "Report Builder"
	assert c.changes[0]["data"]["ref_doctype"] == "Customer"
	assert "query" not in c.changes[0]["data"]
