"""Tests for the single-agent lite pipeline (build_lite_crew).

Verifies crew shape, task naming (for run_crew compatibility), agent role
(for UI phase_map compatibility), and tool assignment. Doesn't actually run
the crew (that needs a live LLM) - those are manual-QA smoke tests.
"""

import os
import pytest
from crewai import Process

from alfred.agents.crew import build_lite_crew, LITE_TASK_DESCRIPTION


@pytest.fixture(autouse=True)
def set_llm_env():
	os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
	yield


class TestLiteCrewShape:
	"""The lite crew must produce a 1-agent / 1-task crew that run_crew
	can process without any lite-specific branching."""

	def test_builds_without_errors(self):
		crew, state = build_lite_crew("Create a notification for leave applications")
		assert crew is not None
		assert state is not None

	def test_single_agent(self):
		crew, _ = build_lite_crew("Test prompt")
		assert len(crew.agents) == 1

	def test_single_task(self):
		crew, _ = build_lite_crew("Test prompt")
		assert len(crew.tasks) == 1

	def test_sequential_process(self):
		crew, _ = build_lite_crew("Test prompt")
		assert crew.process == Process.sequential

	def test_no_manager_agent(self):
		crew, _ = build_lite_crew("Test prompt")
		assert crew.manager_agent is None

	def test_memory_disabled(self):
		"""Lite mode skips vector memory to minimize overhead."""
		crew, _ = build_lite_crew("Test prompt")
		assert crew.memory is False


class TestLiteCrewAgentRole:
	"""The lite agent uses role='Frappe Developer' so run_crew's phase_map
	and the UI's AGENT_PHASE_MAP both map correctly without any branching."""

	def test_agent_role_is_frappe_developer(self):
		crew, _ = build_lite_crew("Test")
		agent = crew.agents[0]
		assert agent.role == "Frappe Developer"

	def test_agent_delegation_disabled(self):
		crew, _ = build_lite_crew("Test")
		assert crew.agents[0].allow_delegation is False

	def test_agent_has_lite_backstory(self):
		crew, _ = build_lite_crew("Test")
		backstory = crew.agents[0].backstory
		assert "Alfred Lite" in backstory
		assert "get_doctype_schema" in backstory  # must reference MCP tool


class TestLiteTaskShape:
	"""The single task must be named 'generate_changeset' so run_crew's
	changeset extraction picks up its output as the final changeset."""

	def test_task_name_maps_to_generate_changeset(self):
		crew, _ = build_lite_crew("Test prompt")
		# The run_crew integration relies on _alfred_task_names being set
		# so the extraction logic can find "generate_changeset" and treat
		# the lite task's output as the changeset source.
		assert hasattr(crew, "_alfred_task_names")
		assert crew._alfred_task_names == ["generate_changeset"]

	def test_task_description_contains_prompt(self):
		crew, _ = build_lite_crew("Create a Book DocType")
		desc = crew.tasks[0].description
		assert "Book" in desc

	def test_task_human_input_disabled(self):
		"""Lite mode has no UI bridge for human_input, must be False."""
		crew, _ = build_lite_crew("Test")
		assert crew.tasks[0].human_input is False

	def test_task_description_mentions_minimal_change(self):
		"""The description must tell the agent to prefer the smallest change."""
		assert "minimal" in LITE_TASK_DESCRIPTION.lower()

	def test_task_description_mandates_mcp_verification(self):
		"""Without this, the lite agent hallucinates field names."""
		assert "get_doctype_schema" in LITE_TASK_DESCRIPTION


class TestLiteCrewWithTools:
	"""Lite agent tool assignment."""

	def test_accepts_lite_tools(self):
		from crewai.tools import tool

		@tool
		def my_fake_tool() -> str:
			"""Fake tool for test."""
			return ""

		crew, _ = build_lite_crew("Test", lite_tools=[my_fake_tool])
		assert len(crew.agents[0].tools) == 1
		assert crew.agents[0].tools[0].name == "my_fake_tool"

	def test_empty_tools_is_tolerated(self):
		"""Agent can be built with no tools (will be blind but doesn't crash)."""
		crew, _ = build_lite_crew("Test", lite_tools=[])
		assert len(crew.agents[0].tools) == 0

	def test_none_tools_defaults_to_empty(self):
		crew, _ = build_lite_crew("Test", lite_tools=None)
		assert len(crew.agents[0].tools) == 0


class TestLiteCrewState:
	"""State handling is shared with the full crew - same CrewState class."""

	def test_fresh_state_when_no_previous(self):
		_, state = build_lite_crew("Test")
		assert state.completed_tasks == {}
		assert state.current_phase == "gather_requirements"
		assert state.dry_run_retries == 0

	def test_passes_through_previous_state(self):
		from alfred.agents.crew import CrewState
		prev = CrewState()
		prev.dry_run_retries = 1
		_, state = build_lite_crew("Test", previous_state=prev)
		# Same object returned - state isn't reset on lite build
		assert state is prev
		assert state.dry_run_retries == 1
