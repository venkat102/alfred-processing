"""Tests for build_plan_crew (Phase C of three-mode chat).

Covers:
  - Crew has exactly 3 agents (Requirement, Assessment, Architect)
  - Final task is named generate_plan_doc (not generate_changeset)
  - Tool assignments reach the correct agents
  - Task description mandates JSON output + forbids code
  - Agents used are from build_agents (reused, not duplicated)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestBuildPlanCrew:
	def test_crew_has_three_agents(self):
		from alfred.agents.plan_crew import build_plan_crew

		crew, _ = build_plan_crew(
			user_prompt="how would we add approval to Expense Claims?",
			user_context={"user": "tester", "roles": ["System Manager"]},
			site_config={"llm_model": "ollama/llama3.1"},
			custom_tools=None,
		)
		assert len(crew.agents) == 3

	def test_task_names_end_with_generate_plan_doc(self):
		from alfred.agents.plan_crew import build_plan_crew

		crew, _ = build_plan_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			custom_tools=None,
		)

		# The crew announces its task names via _alfred_task_names so the
		# pipeline can distinguish a plan run from a dev run.
		assert getattr(crew, "_alfred_task_names", None) == [
			"gather_requirements",
			"assess_feasibility",
			"generate_plan_doc",
		]

	def test_crew_has_three_tasks(self):
		from alfred.agents.plan_crew import build_plan_crew

		crew, _ = build_plan_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			custom_tools=None,
		)
		assert len(crew.tasks) == 3

	def test_agent_roles_are_planning_agents(self):
		from alfred.agents.plan_crew import build_plan_crew

		crew, _ = build_plan_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			custom_tools=None,
		)
		roles = [a.role for a in crew.agents]
		# All three planning roles must be present; no Developer / Tester /
		# Deployer in Plan mode.
		assert any("Requirement" in r for r in roles)
		assert any("Assess" in r or "Feasibility" in r for r in roles)
		assert any("Architect" in r for r in roles)
		assert not any("Developer" in r for r in roles)
		assert not any("Deploy" in r for r in roles)
		assert not any("QA" in r for r in roles)

	def test_plan_doc_task_forbids_code_output(self):
		"""The terminal task description must tell the LLM to output JSON only."""
		from alfred.agents.plan_crew import _PLAN_DOC_TASK_DESCRIPTION

		assert "JSON" in _PLAN_DOC_TASK_DESCRIPTION
		assert "code" in _PLAN_DOC_TASK_DESCRIPTION.lower()
		# Must reference the expected keys
		for key in ("title", "summary", "steps", "doctypes_touched", "risks", "open_questions"):
			assert key in _PLAN_DOC_TASK_DESCRIPTION

	def test_plan_doc_task_limits_step_count(self):
		"""Task must prevent unbounded plan step lists."""
		from alfred.agents.plan_crew import _PLAN_DOC_TASK_DESCRIPTION

		# Either explicit "12" cap or "between 1 and 8" range
		assert "12" in _PLAN_DOC_TASK_DESCRIPTION or "8" in _PLAN_DOC_TASK_DESCRIPTION

	def test_custom_tools_flow_through_to_build_agents(self):
		"""Tool assignments must reach the underlying build_agents call.

		plan_crew.py does `from alfred.agents.definitions import build_agents`
		and then calls `build_agents(...)` - so we patch the name in
		definitions (which plan_crew's call resolves against since the
		import is inline inside build_plan_crew).
		"""
		from alfred.agents.definitions import build_agents as real_build
		from alfred.agents.plan_crew import build_plan_crew

		fake_tools = {
			"requirement": [],
			"assessment": [],
			"architect": [],
			"developer": [],
			"tester": [],
			"deployer": [],
		}

		captured = {}

		def wrapper(**kwargs):
			captured.update(kwargs)
			return real_build(**kwargs)

		with patch("alfred.agents.definitions.build_agents", side_effect=wrapper):
			build_plan_crew(
				user_prompt="q",
				user_context={},
				site_config={"llm_model": "ollama/llama3.1"},
				custom_tools=fake_tools,
			)

		assert captured.get("custom_tools") is fake_tools

	def test_runs_without_custom_tools(self):
		"""Should not crash when MCP client is absent (unit-test path)."""
		from alfred.agents.plan_crew import build_plan_crew

		crew, state = build_plan_crew(
			user_prompt="q",
			user_context={},
			site_config={"llm_model": "ollama/llama3.1"},
			custom_tools=None,
		)
		assert crew is not None
		assert state is not None
