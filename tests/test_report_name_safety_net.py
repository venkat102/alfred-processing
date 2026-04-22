"""V4 safety net: Report changesets get report_name from the handoff
candidate's suggested_name when the specialist's output omitted it.
"""

from unittest.mock import MagicMock

import pytest

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
	"""Mirror the post_crew safety-net block inline for direct testing."""
	if not ctx.changes:
		return
	if ctx.intent != "create_report":
		return
	if not isinstance(ctx.report_candidate, dict):
		return
	suggested_name = ctx.report_candidate.get("suggested_name")
	if not suggested_name:
		return
	for item in ctx.changes:
		if item.get("doctype") != "Report":
			continue
		data = item.setdefault("data", {})
		if not data.get("report_name"):
			data["report_name"] = suggested_name
			meta = item.setdefault("field_defaults_meta", {})
			meta["report_name"] = {
				"source": "default",
				"rationale": "filled-from-handoff",
			}


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
