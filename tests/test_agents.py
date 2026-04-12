"""Tests for CrewAI agent definitions."""

import os

import pytest

from alfred.agents import backstories
from alfred.agents.definitions import build_agents, check_llm_health, _resolve_llm
from alfred.agents.tool_stubs import TOOL_ASSIGNMENTS


class TestAgentInstantiation:
	"""Test that all agents can be created without errors."""

	# Expected 6 agents after the sequential-process refactor.
	# The orchestrator was removed when Process.hierarchical was replaced by
	# Process.sequential to eliminate delegation loops with local LLMs.
	EXPECTED_AGENTS = {
		"requirement", "assessment", "architect",
		"developer", "tester", "deployer",
	}

	def test_all_agents_instantiate(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		assert len(agents) == len(self.EXPECTED_AGENTS)
		assert set(agents.keys()) == self.EXPECTED_AGENTS

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

	def test_delegation_disabled(self):
		"""Sequential-process agents must not delegate - it triggers infinite
		loops with local LLMs that handle the delegation prompt poorly."""
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		for name, agent in agents.items():
			assert agent.allow_delegation is False, f"Agent '{name}' must not allow delegation"


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
		# max_tokens > 4096 is capped at 4096 for Ollama models to prevent
		# KV-cache OOM on large local models (llama3.3:70b etc.). See
		# _resolve_llm's Ollama cap. Use a non-Ollama model to test the
		# full 8192 path.
		llm_ollama = _resolve_llm({
			"llm_model": "ollama/llama3.1",
			"llm_temperature": 0.5,
			"llm_max_tokens": 8192,
		})
		assert llm_ollama.temperature == 0.5
		assert llm_ollama.max_tokens == 4096  # capped

		llm_cloud = _resolve_llm({
			"llm_model": "anthropic/claude-sonnet-4-20250514",
			"llm_temperature": 0.3,
			"llm_max_tokens": 8192,
		})
		assert llm_cloud.temperature == 0.3
		assert llm_cloud.max_tokens == 8192  # not capped for cloud


class TestToolAssignments:
	"""Test the minimal local-only TOOL_ASSIGNMENTS fallback used when no
	custom_tools (i.e. MCP-backed tools) are passed to build_agents.

	Production always passes MCP tools via build_mcp_tools(); this fallback
	exists for unit tests and offline dev. Most Frappe-backed stubs were
	removed in task A5 since they were misleading hardcoded data.
	"""

	def test_requirement_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["requirement"].tools}
		# Minimal fallback only has ask_user (local, no Frappe dependency)
		assert "ask_user" in tool_names

	def test_tester_agent_tools(self):
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		tool_names = {t.name for t in agents["tester"].tools}
		# Minimal fallback: only local syntax validators
		assert "validate_python_syntax" in tool_names
		assert "validate_js_syntax" in tool_names

	def test_most_agents_have_no_local_tools(self):
		"""Assessment, architect, developer, deployer rely entirely on MCP in
		production. Their fallback tool lists are empty."""
		os.environ["FALLBACK_LLM_MODEL"] = "ollama/llama3.1"
		agents = build_agents()
		for key in ("assessment", "architect", "developer", "deployer"):
			assert len(agents[key].tools) == 0, f"{key} should have no fallback tools"


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

	# ORCHESTRATOR_AGENT is dead code (reserved for future Process.hierarchical
	# re-enable) and intentionally unused by build_agents today, but still
	# validated here so re-enabling it later isn't blocked by a rotted prompt.
	# LITE_AGENT is the single-agent fused backstory for the lite pipeline.
	ALL_BACKSTORIES = [
		"REQUIREMENT_AGENT", "ASSESSMENT_AGENT", "ARCHITECT_AGENT",
		"DEVELOPER_AGENT", "TESTER_AGENT", "DEPLOYER_AGENT",
		"ORCHESTRATOR_AGENT", "LITE_AGENT",
	]

	def test_backstories_not_empty(self):
		for name in self.ALL_BACKSTORIES:
			backstory = getattr(backstories, name)
			assert backstory, f"{name} backstory is empty"
			assert len(backstory) > 200, f"{name} backstory is too short ({len(backstory)} chars)"

	def test_backstories_contain_negative_constraints(self):
		"""Each backstory must say what the agent must NOT do."""
		for name in self.ALL_BACKSTORIES:
			backstory = getattr(backstories, name)
			assert "MUST NOT" in backstory or "NOT" in backstory, \
				f"{name} backstory should contain negative constraints"

	def test_assessment_backstory_requires_tool_usage(self):
		assert "ALWAYS use the check_permission tool" in backstories.ASSESSMENT_AGENT

	def test_developer_backstory_requires_permission_checks(self):
		assert "permission checks" in backstories.DEVELOPER_AGENT.lower()
		assert "Alfred" in backstories.DEVELOPER_AGENT

	def test_lite_backstory_instructs_live_verification(self):
		"""Lite agent has no Tester - must verify against live site via MCP."""
		assert "get_doctype_schema" in backstories.LITE_AGENT
		assert "verify" in backstories.LITE_AGENT.lower()


class TestHealthCheck:
	def test_health_check_returns_dict(self):
		result = check_llm_health({"llm_model": "ollama/llama3.1"})
		assert "healthy" in result
		assert "model" in result
