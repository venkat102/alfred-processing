"""Tests for CrewAI Crew orchestration."""

import json
import os

import pytest

from alfred.agents.crew import (
	TASK_DESCRIPTIONS,
	CrewState,
	build_alfred_crew,
	load_crew_state,
	save_crew_state,
)


@pytest.fixture(autouse=True)
def set_llm_env():
	os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
	yield


class TestCrewBuild:
	"""Test crew construction."""

	def test_crew_builds_without_errors(self):
		crew, state = build_alfred_crew("Create a DocType called Book", {"user": "admin@test.com"})
		assert crew is not None
		assert state is not None
		assert len(crew.tasks) == 6  # All 6 SDLC tasks

	def test_crew_has_all_agents(self):
		crew, _ = build_alfred_crew("Create a ToDo app")
		agent_roles = {a.role for a in crew.agents}
		# All 6 specialists are in the crew's agents list (no manager agent
		# in sequential mode - orchestrator was removed to eliminate
		# delegation loops with local LLMs).
		assert "Requirement Analyst" in agent_roles
		assert "Feasibility Assessor" in agent_roles
		assert "Solution Architect" in agent_roles
		assert "Frappe Developer" in agent_roles
		assert "QA Validator" in agent_roles
		assert "Deployment Specialist" in agent_roles
		assert "Orchestrator" not in agent_roles
		assert crew.manager_agent is None

	def test_crew_uses_sequential_process(self):
		"""Sequential (not hierarchical) since switching eliminated the
		delegation loop problem with Ollama-backed agents."""
		from crewai import Process
		crew, _ = build_alfred_crew("Test prompt")
		assert crew.process == Process.sequential

	def test_crew_has_no_manager_agent(self):
		"""Sequential process doesn't use a manager agent."""
		crew, _ = build_alfred_crew("Test prompt")
		assert crew.manager_agent is None

	def test_crew_with_site_config(self):
		crew, _ = build_alfred_crew(
			"Test prompt",
			site_config={"llm_model": "ollama/mistral", "max_retries_per_agent": 5},
		)
		assert crew is not None

	def test_task_descriptions_are_formatted(self):
		crew, _ = build_alfred_crew("Create a Book DocType", {"user": "admin@test.com"})
		# First task should contain the prompt
		first_task = crew.tasks[0]
		assert "Book" in first_task.description

	def test_human_input_disabled_on_all_tasks(self):
		"""All task.human_input must be False: CrewAI's built-in stdin input()
		blocks the worker thread and we don't have a WS bridge wired yet.
		Re-enable once human_input_handler routes through the WebSocket."""
		crew, _ = build_alfred_crew("Test")
		for i, task in enumerate(crew.tasks):
			assert task.human_input is False, f"Task {i} must have human_input=False"


class TestCrewState:
	"""Test state serialization/deserialization."""

	def test_state_creation(self):
		state = CrewState()
		assert state.current_phase == "gather_requirements"
		assert state.delegation_count == 0
		assert state.completed_tasks == {}

	def test_mark_task_complete(self):
		state = CrewState()
		state.mark_task_complete("gather_requirements", "requirements output")
		assert "gather_requirements" in state.completed_tasks
		assert state.completed_tasks["gather_requirements"]["output"] == "requirements output"

	def test_serialization_roundtrip(self):
		state = CrewState()
		state.mark_task_complete("gather_requirements", "req output")
		state.mark_task_complete("assess_feasibility", "assessment output")
		state.delegation_count = 2
		state.current_phase = "design_solution"

		# Serialize to dict
		data = state.to_dict()
		assert isinstance(data, dict)

		# Deserialize
		restored = CrewState.from_dict(data)
		assert restored.current_phase == "design_solution"
		assert restored.delegation_count == 2
		assert "gather_requirements" in restored.completed_tasks
		assert "assess_feasibility" in restored.completed_tasks

	def test_serialization_to_json(self):
		state = CrewState()
		state.mark_task_complete("gather_requirements", '{"objective": "Create a Book DocType"}')
		data = state.to_dict()
		json_str = json.dumps(data)
		assert json_str  # Should be valid JSON
		restored_data = json.loads(json_str)
		restored = CrewState.from_dict(restored_data)
		assert "gather_requirements" in restored.completed_tasks

	def test_delegation_counter(self):
		state = CrewState()
		state.max_delegations = 3
		assert state.increment_delegation() is True  # 1
		assert state.increment_delegation() is True  # 2
		assert state.increment_delegation() is True  # 3
		assert state.increment_delegation() is False  # 4 > 3


class TestCrewResumption:
	"""Test that crews can be rebuilt from saved state."""

	def test_resume_skips_completed_tasks(self):
		# Simulate: first 2 tasks completed
		state = CrewState()
		state.mark_task_complete("gather_requirements", "req output")
		state.mark_task_complete("assess_feasibility", "assessment output")

		crew, new_state = build_alfred_crew(
			"Test prompt",
			previous_state=state,
		)

		# Should have 4 remaining tasks (not 6)
		assert len(crew.tasks) == 4

	def test_resume_preserves_state(self):
		state = CrewState()
		state.mark_task_complete("gather_requirements", "req output")
		state.delegation_count = 2

		_, new_state = build_alfred_crew("Test", previous_state=state)
		assert new_state.delegation_count == 2
		assert "gather_requirements" in new_state.completed_tasks


class TestRedisStatePersistence:
	"""Test saving/loading crew state to Redis."""

	@pytest.fixture
	async def store(self):
		import redis.asyncio as aioredis

		from alfred.state.store import StateStore

		try:
			client = aioredis.from_url("redis://127.0.0.1:11000/2", decode_responses=True)
			await client.ping()
		except Exception:
			pytest.skip("Redis not available")

		store = StateStore(client)
		yield store

		# Cleanup
		async for key in client.scan_iter("alfred:*"):
			await client.delete(key)
		await client.aclose()

	async def test_save_and_load_state(self, store):
		state = CrewState()
		state.mark_task_complete("gather_requirements", "test output")
		state.current_phase = "assess_feasibility"

		await save_crew_state(store, "test-site", "conv-123", state)
		loaded = await load_crew_state(store, "test-site", "conv-123")

		assert loaded is not None
		assert loaded.current_phase == "assess_feasibility"
		assert "gather_requirements" in loaded.completed_tasks

	async def test_load_nonexistent_state(self, store):
		loaded = await load_crew_state(store, "test-site", "nonexistent")
		assert loaded is None


class TestTaskDescriptions:
	"""Test that all task descriptions are complete and well-formed."""

	def test_all_tasks_have_descriptions(self):
		expected_tasks = [
			"gather_requirements", "assess_feasibility", "design_solution",
			"generate_changeset", "validate_changeset", "deploy_changeset",
		]
		for task_name in expected_tasks:
			assert task_name in TASK_DESCRIPTIONS, f"Missing description for {task_name}"
			assert "description" in TASK_DESCRIPTIONS[task_name]
			assert "expected_output" in TASK_DESCRIPTIONS[task_name]

	def test_descriptions_not_empty(self):
		for name, desc in TASK_DESCRIPTIONS.items():
			assert len(desc["description"]) > 50, f"{name} description too short"
			assert len(desc["expected_output"]) > 30, f"{name} expected_output too short"
