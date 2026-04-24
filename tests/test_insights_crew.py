"""Tests for build_insights_crew and the insights tool assignments.

Covers:
  - Crew has exactly 1 agent + 1 task
  - Agent role is distinct from "Frappe Developer" (so UI can badge it)
  - Task name is generate_insights_reply (not generate_changeset)
  - Tools passed through intact
  - Task description includes the budget warning
  - build_mcp_tools returns an "insights" key with the expected tools
  - dry_run_changeset is NOT in the insights tool set
"""

from __future__ import annotations

from unittest.mock import MagicMock


class TestBuildInsightsCrew:
	def test_crew_has_one_agent_one_task(self):
		from alfred.agents.crew import build_insights_crew

		crew, state = build_insights_crew(
			user_prompt="what DocTypes do I have?",
			user_context={"user": "tester", "roles": []},
			site_config={"llm_model": "ollama/llama3.1"},
			insights_tools=[],
		)

		assert len(crew.agents) == 1
		assert len(crew.tasks) == 1

	def test_task_name_is_generate_insights_reply(self):
		from alfred.agents.crew import build_insights_crew

		crew, _ = build_insights_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			insights_tools=[],
		)

		assert getattr(crew, "_alfred_task_names", None) == ["generate_insights_reply"]

	def test_agent_role_is_insights_specialist(self):
		from alfred.agents.crew import build_insights_crew

		crew, _ = build_insights_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			insights_tools=[],
		)

		role = crew.agents[0].role
		# Must be distinct from the dev-mode "Frappe Developer" role so the
		# UI can badge it differently.
		assert role != "Frappe Developer"
		assert "site" in role.lower() or "insights" in role.lower() or "information" in role.lower()

	def test_tools_passed_through(self):
		"""The agent must receive exactly the tools we hand in.

		CrewAI validates tool types via pydantic so we need real CrewAI
		tool objects, not MagicMocks. Use the @tool decorator to build
		two trivial tool instances.
		"""
		from crewai.tools import tool as crewai_tool

		from alfred.agents.crew import build_insights_crew

		@crewai_tool
		def _fake_tool_a(arg: str = "") -> str:
			"""Fake tool A for testing."""
			return ""

		@crewai_tool
		def _fake_tool_b(arg: str = "") -> str:
			"""Fake tool B for testing."""
			return ""

		crew, _ = build_insights_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			insights_tools=[_fake_tool_a, _fake_tool_b],
		)

		assert len(crew.agents[0].tools) == 2
		tool_names = {getattr(t, "name", None) for t in crew.agents[0].tools}
		assert "_fake_tool_a" in tool_names
		assert "_fake_tool_b" in tool_names

	def test_task_description_mentions_read_only(self):
		from alfred.agents.crew import INSIGHTS_TASK_DESCRIPTION

		# Must make it unambiguous to the LLM that this is read-only
		assert "READ-ONLY" in INSIGHTS_TASK_DESCRIPTION
		# Must tell the LLM not to produce JSON/changesets
		assert "JSON" in INSIGHTS_TASK_DESCRIPTION or "changeset" in INSIGHTS_TASK_DESCRIPTION.lower()
		# Must mention the budget cap
		assert "5" in INSIGHTS_TASK_DESCRIPTION

	def test_task_description_lists_allowed_tools(self):
		from alfred.agents.crew import INSIGHTS_TASK_DESCRIPTION

		# Sanity check that the agent knows which tools are available
		assert "lookup_doctype" in INSIGHTS_TASK_DESCRIPTION


class TestInsightsToolAssignments:
	def test_build_mcp_tools_returns_insights_key(self):
		from alfred.tools.mcp_tools import build_mcp_tools

		mcp_client = MagicMock()
		tools = build_mcp_tools(mcp_client)
		assert "insights" in tools
		assert isinstance(tools["insights"], list)
		assert len(tools["insights"]) > 0

	def test_insights_excludes_dry_run_changeset(self):
		from alfred.tools.mcp_tools import build_mcp_tools

		mcp_client = MagicMock()
		tools = build_mcp_tools(mcp_client)
		insights = tools["insights"]

		# dry_run_changeset is a deploy-shaped tool and must NOT be in
		# the Insights set
		names = [getattr(t, "name", "") for t in insights]
		assert "dry_run_changeset" not in names

	def test_insights_excludes_local_stubs(self):
		"""The local Python/JS syntax stubs are dev-only validators."""
		from alfred.tools.mcp_tools import build_mcp_tools

		mcp_client = MagicMock()
		tools = build_mcp_tools(mcp_client)
		names = [getattr(t, "name", "") for t in tools["insights"]]

		assert "validate_python_syntax_stub" not in names
		assert "validate_js_syntax_stub" not in names
		assert "ask_user_stub" not in names

	def test_insights_includes_core_readonly_tools(self):
		from alfred.tools.mcp_tools import build_mcp_tools

		mcp_client = MagicMock()
		tools = build_mcp_tools(mcp_client)
		names = [getattr(t, "name", "") for t in tools["insights"]]

		# Core read-only tools an insights agent should have
		expected = {
			"lookup_doctype",
			"lookup_pattern",
			"get_site_info",
			"get_doctypes",
			"get_existing_customizations",
			"get_user_context",
			"check_permission",
			"has_active_workflow",
			"check_has_records",
		}
		for tool in expected:
			assert tool in names, f"Insights tool set missing {tool}"


class TestInsightsTaskDescriptionTemplate:
	"""Regression guard: the task-description template uses str.format() with
	only two named placeholders (prompt, user_context). Any literal `{` or `}`
	elsewhere in the template (e.g. JSON example payloads for run_query /
	get_list) must be doubled to `{{` / `}}` or .format() raises KeyError and
	the whole crew fails to build - surfaced to the user as "I wasn't able to
	spin up the Insights agent just now."
	"""

	def test_template_formats_cleanly_with_production_inputs(self):
		from alfred.agents.crew import INSIGHTS_TASK_DESCRIPTION

		# The exact two placeholders the handler passes in production.
		out = INSIGHTS_TASK_DESCRIPTION.format(
			prompt="list all active customers",
			user_context='{"user": "tester", "roles": ["System Manager"]}',
		)
		assert "list all active customers" in out
		assert '"user": "tester"' in out

	def test_template_preserves_json_examples(self):
		"""The JSON examples in the template must survive .format() intact -
		they're there to teach the LLM how to shape tool calls."""
		from alfred.agents.crew import INSIGHTS_TASK_DESCRIPTION

		out = INSIGHTS_TASK_DESCRIPTION.format(prompt="q", user_context="{}")
		# get_list filter example
		assert '{"disabled": 0}' in out
		assert '{"status": "Unpaid"}' in out
		# run_query aggregation example
		assert '"from_doctype": "Sales Invoice"' in out
		assert '{"field": "customer"}' in out
		assert '{"field": "grand_total"' in out

	def test_build_insights_crew_does_not_raise_on_format(self):
		"""End-to-end smoke: the full crew build path must not raise on
		template format - the handler wraps build_insights_crew in a try/except
		that surfaces a generic "spin up" error, which masks the real cause.
		"""
		from alfred.agents.crew import build_insights_crew

		crew, state = build_insights_crew(
			user_prompt="show me customers",
			user_context={"user": "tester", "roles": []},
			site_config={"llm_model": "ollama/llama3.1"},
			insights_tools=[],
		)
		assert crew is not None
		assert len(crew.tasks) == 1
		desc = crew.tasks[0].description
		assert "show me customers" in desc
		assert '{"disabled": 0}' in desc
