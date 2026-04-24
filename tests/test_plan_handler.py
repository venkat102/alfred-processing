"""Tests for the Plan mode handler (Phase C of three-mode chat).

Covers:
  - Successful run returns a validated PlanDoc-shaped dict
  - Output JSON parser handles prose wrapping, code fences, extra text
  - Pydantic validation catches malformed output and returns a stub
  - run_crew failure returns a stub dict, never raises
  - Non-completed crew status returns a stub
  - Missing MCP client still produces a plan (just without tools)
  - init_run_state is called with the plan-mode budget (15)
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from alfred.handlers.plan import (
	_PLAN_TOOL_BUDGET,
	_parse_plan_doc_json,
	_strip_code_fences,
	_validate_as_plan_doc,
	handle_plan,
)
from alfred.models.plan_doc import PlanDoc


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


def _make_conn(with_mcp: bool = True) -> MagicMock:
	conn = MagicMock()
	conn.site_id = "test-site"
	conn.user = "tester@example.com"
	conn.roles = ["System Manager"]
	conn.site_config = {"llm_model": "ollama/llama3.1"}
	conn.mcp_client = MagicMock() if with_mcp else None
	return conn


VALID_PLAN = {
	"title": "Approval workflow for Expense Claims",
	"summary": "Add a 2-step approval: manager, then finance.",
	"steps": [
		{"order": 1, "action": "Create Workflow 'Expense Claim Approval'", "rationale": "need approval", "doctype": "Workflow"},
		{"order": 2, "action": "Create Notification for approvers", "rationale": "notify them", "doctype": "Notification"},
	],
	"doctypes_touched": ["Workflow", "Notification"],
	"risks": ["Submitted records would need to be re-submitted."],
	"open_questions": ["Who approves when manager is absent?"],
	"estimated_items": 2,
}


class TestStripCodeFences:
	def test_strips_json_fences(self):
		text = "```json\n{\"a\": 1}\n```"
		assert _strip_code_fences(text) == '{"a": 1}'

	def test_strips_bare_fences(self):
		text = "```\n{\"a\": 1}\n```"
		assert _strip_code_fences(text) == '{"a": 1}'

	def test_leaves_unfenced_text(self):
		assert _strip_code_fences('{"a": 1}') == '{"a": 1}'

	def test_empty_string(self):
		assert _strip_code_fences("") == ""


class TestParsePlanDocJson:
	def test_clean_json(self):
		parsed = _parse_plan_doc_json(json.dumps(VALID_PLAN))
		assert parsed["title"] == VALID_PLAN["title"]

	def test_fenced_json(self):
		text = "```json\n" + json.dumps(VALID_PLAN) + "\n```"
		parsed = _parse_plan_doc_json(text)
		assert parsed["title"] == VALID_PLAN["title"]

	def test_prose_before_json(self):
		text = "Here is the plan:\n" + json.dumps(VALID_PLAN)
		parsed = _parse_plan_doc_json(text)
		assert parsed["title"] == VALID_PLAN["title"]

	def test_prose_after_json(self):
		text = json.dumps(VALID_PLAN) + "\n\nThat's the plan."
		parsed = _parse_plan_doc_json(text)
		assert parsed["title"] == VALID_PLAN["title"]

	def test_garbage_returns_none(self):
		assert _parse_plan_doc_json("not json at all") is None

	def test_empty_returns_none(self):
		assert _parse_plan_doc_json("") is None


class TestValidateAsPlanDoc:
	def test_valid_plan_passes(self):
		out = _validate_as_plan_doc(VALID_PLAN, user_prompt="...")
		assert out["title"] == VALID_PLAN["title"]
		assert len(out["steps"]) == 2

	def test_invalid_plan_falls_back_to_stub(self):
		# Missing required title/summary
		out = _validate_as_plan_doc({"randomkey": "value"}, user_prompt="...")
		assert "could not be parsed" in out["title"].lower()
		assert out["summary"]
		# Salvaged raw output should appear in open_questions
		assert any("raw" in q.lower() for q in out["open_questions"])

	def test_missing_required_fields_falls_back(self):
		# Has title but no summary
		out = _validate_as_plan_doc({"title": "X"}, user_prompt="...")
		assert "could not be parsed" in out["title"].lower() or out["title"] == "X"


class TestHandlePlan:
	def test_returns_validated_plan_on_success(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {"status": "completed", "result": json.dumps(VALID_PLAN)}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="how would we add approval to Expense Claims?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		assert out["title"] == VALID_PLAN["title"]
		assert len(out["steps"]) == 2
		assert "Workflow" in out["doctypes_touched"]

	def test_init_run_state_uses_plan_budget(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {"status": "completed", "result": json.dumps(VALID_PLAN)}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state") as init_state, \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			_run(
				handle_plan(
					prompt="plan question",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		assert init_state.called
		kwargs = init_state.call_args.kwargs
		assert kwargs["budget"] == _PLAN_TOOL_BUDGET
		assert _PLAN_TOOL_BUDGET == 15  # sanity - plan sits between insights(5) and dev(30)

	def test_fenced_output_is_parsed(self):
		conn = _make_conn()

		fenced = "```json\n" + json.dumps(VALID_PLAN) + "\n```"

		async def fake_run(**kwargs):
			return {"status": "completed", "result": fenced}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		assert out["title"] == VALID_PLAN["title"]

	def test_empty_result_returns_stub(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {"status": "completed", "result": "   "}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		# Falls back to stub
		assert "unreadable" in out["title"].lower() or "incomplete" in out["title"].lower() or "parsed" in out["summary"].lower()

	def test_run_crew_failure_returns_stub(self):
		conn = _make_conn()

		async def boom(**kwargs):
			raise RuntimeError("crew fire")

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=boom):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		# Must return a valid PlanDoc-shaped dict even on failure
		validated = PlanDoc.model_validate(out)
		assert validated is not None
		assert "fire" in out["summary"] or "error" in out["summary"].lower() or "failed" in out["title"].lower()

	def test_build_plan_crew_failure_returns_stub(self):
		conn = _make_conn()

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", side_effect=ValueError("bad build")):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		# Never raises - returns a valid stub
		validated = PlanDoc.model_validate(out)
		assert validated is not None

	def test_no_mcp_client_still_runs(self):
		conn = _make_conn(with_mcp=False)

		async def fake_run(**kwargs):
			return {"status": "completed", "result": json.dumps(VALID_PLAN)}

		with patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		assert out["title"] == VALID_PLAN["title"]

	def test_non_completed_status_returns_stub(self):
		conn = _make_conn()

		async def failed_run(**kwargs):
			return {"status": "failed", "error": "crew state went bad"}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=failed_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		validated = PlanDoc.model_validate(out)
		assert validated is not None
		assert "crew state went bad" in out["summary"] or "incomplete" in out["title"].lower()


# ── #FLOW4: parse_failed flag is set on every stub path ──────────────


class TestParseFailedFlag:
	"""Every stub-returning path must set parse_failed=True so the UI can
	tell a real plan apart from an error-fallback. A real plan has
	parse_failed=False (default from the model)."""

	def test_successful_plan_has_parse_failed_false(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {"status": "completed", "result": json.dumps(VALID_PLAN)}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert out["parse_failed"] is False
		assert out["parse_failure_detail"] is None

	def test_json_parse_failure_sets_flag_and_detail(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {"status": "completed", "result": "this is not json at all {{{{"}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert out["parse_failed"] is True
		assert "JSON parse failed" in out["parse_failure_detail"]
		assert "this is not json" in out["parse_failure_detail"]

	def test_schema_validation_failure_sets_flag_and_detail(self):
		# _validate_as_plan_doc gets a dict that parses as JSON but
		# doesn't match PlanDoc shape - missing required fields.
		out = _validate_as_plan_doc(
			{"some_random_key": "nope"}, user_prompt="q",
		)
		assert out["parse_failed"] is True
		assert "Schema validation failed" in out["parse_failure_detail"]

	def test_crew_run_exception_sets_flag_and_detail(self):
		conn = _make_conn()

		async def boom(**kwargs):
			raise RuntimeError("crew exploded")

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=boom):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert out["parse_failed"] is True
		assert "crew exploded" in out["parse_failure_detail"]

	def test_non_completed_status_sets_flag(self):
		conn = _make_conn()

		async def failed_run(**kwargs):
			return {"status": "failed", "error": "crew died"}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=failed_run):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert out["parse_failed"] is True
		assert "crew died" in out["parse_failure_detail"]

	def test_build_crew_failure_sets_flag(self):
		conn = _make_conn()

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.plan_crew.build_plan_crew", side_effect=ValueError("bad builder")):
			out = _run(
				handle_plan(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert out["parse_failed"] is True
		assert "bad builder" in out["parse_failure_detail"]
