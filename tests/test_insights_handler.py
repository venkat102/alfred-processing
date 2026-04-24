"""Tests for the insights mode handler (Phase B of three-mode chat).

Covers:
  - Handler builds a crew with read-only tools from build_mcp_tools(...)["insights"]
  - init_run_state is called with the tight insights budget
  - Successful run returns the crew's result text as markdown
  - Empty result falls back to a friendly message
  - run_crew failure returns a fallback, never raises
  - No mcp_client -> empty tool set, still returns a message
  - Leading/trailing code fences are stripped from the reply
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from alfred.handlers.insights import _INSIGHTS_TOOL_BUDGET, handle_insights


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


class TestHandleInsights:
	def test_returns_crew_result_on_success(self):
		conn = _make_conn()

		async def fake_run(**kwargs):
			return {
				"status": "completed",
				"result": "You have 12 DocTypes in the Selling module: Customer, Sales Order, ...",
			}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": ["tool1", "tool2"]}), \
			 patch("alfred.tools.mcp_tools.init_run_state") as init_state, \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			reply = _run(
				handle_insights(
					prompt="what DocTypes do I have?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester", "roles": []},
				)
			)

		assert "You have 12 DocTypes" in reply
		# Budget must have been set to the tight insights cap
		init_state.assert_called_once()
		call_kwargs = init_state.call_args.kwargs
		assert call_kwargs["budget"] == _INSIGHTS_TOOL_BUDGET
		assert call_kwargs["conversation_id"] == "conv-1"

	def test_uses_insights_tools_from_build_mcp_tools(self):
		conn = _make_conn()

		captured_build_kwargs = {}

		def capture_build(**kwargs):
			captured_build_kwargs.update(kwargs)
			return (MagicMock(), MagicMock())

		async def fake_run(**kwargs):
			return {"status": "completed", "result": "answer"}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": ["tA", "tB", "tC"]}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", side_effect=capture_build), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			_run(
				handle_insights(
					prompt="anything",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)

		assert captured_build_kwargs.get("insights_tools") == ["tA", "tB", "tC"]

	def test_no_mcp_client_still_returns_reply(self):
		conn = _make_conn(with_mcp=False)

		async def fake_run(**kwargs):
			return {"status": "completed", "result": "reply from llm"}

		with patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fake_run):
			reply = _run(
				handle_insights(
					prompt="what modules exist?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert reply == "reply from llm"

	def test_run_failure_returns_fallback(self):
		conn = _make_conn()

		async def boom(**kwargs):
			raise RuntimeError("crew exploded")

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=boom):
			reply = _run(
				handle_insights(
					prompt="what DocTypes do I have?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert reply is not None
		assert "try again" in reply.lower() or "error" in reply.lower()

	def test_empty_result_returns_friendly_fallback(self):
		conn = _make_conn()

		async def empty(**kwargs):
			return {"status": "completed", "result": "   "}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=empty):
			reply = _run(
				handle_insights(
					prompt="weird question",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert "rephrase" in reply.lower() or "didn't get" in reply.lower()

	def test_non_completed_status_returns_fallback(self):
		conn = _make_conn()

		async def failed_run(**kwargs):
			return {"status": "failed", "error": "crew state bad"}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=failed_run):
			reply = _run(
				handle_insights(
					prompt="what workflows exist?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert "couldn't" in reply.lower() or "couldn" in reply.lower()
		assert "crew state bad" in reply

	def test_code_fences_are_stripped(self):
		conn = _make_conn()

		async def fenced(**kwargs):
			return {
				"status": "completed",
				"result": "```markdown\nYou have 3 workflows.\n```",
			}

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=fenced):
			reply = _run(
				handle_insights(
					prompt="how many workflows?",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert reply == "You have 3 workflows."

	def test_build_crew_failure_returns_fallback(self):
		conn = _make_conn()

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", side_effect=RuntimeError("bad build")):
			reply = _run(
				handle_insights(
					prompt="anything",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
				)
			)
		assert "Insights agent" in reply
		assert "try again" in reply.lower() or "rephrase" in reply.lower()

	def test_event_callback_threaded_through(self):
		"""event_callback must reach run_crew so the UI gets crew_started events."""
		conn = _make_conn()

		captured = {}

		async def capture_run(**kwargs):
			captured.update(kwargs)
			return {"status": "completed", "result": "ok"}

		async def my_cb(event, data):
			return None

		with patch("alfred.tools.mcp_tools.build_mcp_tools", return_value={"insights": []}), \
			 patch("alfred.tools.mcp_tools.init_run_state"), \
			 patch("alfred.agents.crew.build_insights_crew", return_value=(MagicMock(), MagicMock())), \
			 patch("alfred.agents.crew.run_crew", side_effect=capture_run):
			_run(
				handle_insights(
					prompt="q",
					conn=conn,
					conversation_id="conv-1",
					user_context={"user": "tester"},
					event_callback=my_cb,
				)
			)

		assert captured.get("event_callback") is my_cb
		assert captured.get("conversation_id") == "conv-1"
