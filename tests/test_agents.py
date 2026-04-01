"""Tests for CrewAI agent definitions."""

import os

import pytest

from intern.agents import backstories
from intern.agents.definitions import build_agents, check_llm_health, _resolve_llm
from intern.agents.tool_stubs import TOOL_ASSIGNMENTS


class TestAgentInstantiation:
	"""Test that all agents can be created without errors."""

	def test_all_agents_instantiate(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		assert len(agents) == 7
		assert set(agents.keys()) == {
			"requirement", "assessment", "architect",
			"developer", "tester", "deployer", "orchestrator",
		}

	def test_each_agent_has_unique_role(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		roles = [a.role for a in agents.values()]
		assert len(roles) == len(set(roles)), f"Duplicate roles: {roles}"

	def test_each_agent_has_backstory(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		for name, agent in agents.items():
			assert agent.backstory, f"Agent '{name}' has empty backstory"
			assert len(agent.backstory) > 100, f"Agent '{name}' backstory is too short"

	def test_each_agent_allows_delegation(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		for name, agent in agents.items():
			assert agent.allow_delegation is True, f"Agent '{name}' should allow delegation"


class TestLLMResolution:
	"""Test LLM configuration resolution from various sources."""

	def test_llm_from_site_config(self):
		llm = _resolve_llm({"llm_model": "ollama/mistral"})
		assert llm.model == "ollama/mistral"

	def test_llm_from_env_fallback(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/codellama"
		llm = _resolve_llm({})
		assert llm.model == "ollama/codellama"

	def test_llm_default_when_no_config(self):
		os.environ.pop("FALLBACK_LLM_MODEL", None)
		os.environ.pop("FALLBACK_LLM_API_KEY", None)
		os.environ.pop("FALLBACK_LLM_BASE_URL", None)
		llm = _resolve_llm(None)
		assert llm.model == "ollama/llama3.1"

	def test_llm_site_config_overrides_env(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/codellama"
		llm = _resolve_llm({"llm_model": "anthropic/claude-sonnet-4-20250514"})
		assert llm.model == "anthropic/claude-sonnet-4-20250514"

	def test_llm_temperature_and_tokens(self):
		llm = _resolve_llm({
			"llm_model": "ollama/llama3.1",
			"llm_temperature": 0.5,
			"llm_max_tokens": 8192,
		})
		assert llm.temperature == 0.5
		assert llm.max_tokens == 8192


class TestToolAssignments:
	"""Test that each agent has the correct tools from the design document."""

	def test_requirement_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["requirement"].tools}
		expected = {"ask_user", "get_site_info", "get_doctypes", "get_doctype_schema", "get_existing_customizations"}
		assert tool_names == expected, f"Expected {expected}, got {tool_names}"

	def test_assessment_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["assessment"].tools}
		expected = {"check_permission", "get_user_context", "get_existing_customizations"}
		assert tool_names == expected

	def test_architect_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["architect"].tools}
		expected = {"get_doctype_schema", "get_doctypes", "get_existing_customizations", "has_active_workflow"}
		assert tool_names == expected

	def test_developer_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["developer"].tools}
		expected = {"get_doctype_schema", "get_doctypes"}
		assert tool_names == expected

	def test_tester_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["tester"].tools}
		expected = {
			"validate_python_syntax", "validate_js_syntax", "validate_name_available",
			"check_permission", "has_active_workflow", "get_doctype_schema", "check_has_records",
		}
		assert tool_names == expected

	def test_deployer_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["deployer"].tools}
		expected = {"check_has_records"}
		assert tool_names == expected

	def test_orchestrator_has_no_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		assert len(agents["orchestrator"].tools) == 0


class TestCustomTools:
	"""Test that custom tools can override stub tools."""

	def test_custom_tools_override(self):
		from crewai.tools import tool

		@tool
		def my_custom_tool() -> str:
			"""Custom tool for testing."""
			return "custom"

		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents(custom_tools={"requirement": [my_custom_tool]})
		assert len(agents["requirement"].tools) == 1
		assert agents["requirement"].tools[0].name == "my_custom_tool"


class TestBackstories:
	"""Test backstory content quality."""

	def test_backstories_not_empty(self):
		for name in ["REQUIREMENT_AGENT", "ASSESSMENT_AGENT", "ARCHITECT_AGENT",
					  "DEVELOPER_AGENT", "TESTER_AGENT", "DEPLOYER_AGENT", "ORCHESTRATOR_AGENT"]:
			backstory = getattr(backstories, name)
			assert backstory, f"{name} backstory is empty"
			assert len(backstory) > 200, f"{name} backstory is too short ({len(backstory)} chars)"

	def test_backstories_contain_negative_constraints(self):
		"""Each backstory must say what the agent must NOT do."""
		for name in ["REQUIREMENT_AGENT", "ASSESSMENT_AGENT", "ARCHITECT_AGENT",
					  "DEVELOPER_AGENT", "TESTER_AGENT", "DEPLOYER_AGENT", "ORCHESTRATOR_AGENT"]:
			backstory = getattr(backstories, name)
			assert "MUST NOT" in backstory or "NOT" in backstory, \
				f"{name} backstory should contain negative constraints"

	def test_assessment_backstory_requires_tool_usage(self):
		assert "ALWAYS use the check_permission tool" in backstories.ASSESSMENT_AGENT

	def test_developer_backstory_requires_permission_checks(self):
		assert "permission checks" in backstories.DEVELOPER_AGENT.lower()
		assert "Alfred" in backstories.DEVELOPER_AGENT


class TestHealthCheck:
	def test_health_check_returns_dict(self):
		result = check_llm_health({"llm_model": "ollama/llama3.1"})
		assert "healthy" in result
		assert "model" in result
