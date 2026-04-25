"""CrewAI agent definitions for the Alfred SDLC pipeline.

Each agent is a specialist with a unique role, goal, backstory (system prompt),
and assigned tools. Agents are stateless and instantiated per-conversation
with the appropriate LLM config.

Usage:
    from alfred.agents.definitions import build_agents

    agents = build_agents(site_config={"llm_model": "ollama/llama3.1"})
    requirement_agent = agents["requirement"]
"""

import logging
import os

from crewai import LLM, Agent

from alfred.agents import backstories
from alfred.agents.tool_stubs import TOOL_ASSIGNMENTS

logger = logging.getLogger("alfred.agents")


def _resolve_llm(site_config: dict | None = None) -> LLM:
	"""Resolve the LLM to use based on per-connection config or fallback env vars.

	Priority:
	1. site_config["llm_model"] (sent by client app at WebSocket handshake)
	2. FALLBACK_LLM_MODEL environment variable
	3. Default: ollama/llama3.1 (local, free)

	Args:
		site_config: Per-connection settings from the client app.

	Returns:
		A CrewAI LLM instance configured for the resolved provider.
	"""
	config = site_config or {}

	model = config.get("llm_model") or os.environ.get("FALLBACK_LLM_MODEL") or "ollama/llama3.1"
	api_key = config.get("llm_api_key") or os.environ.get("FALLBACK_LLM_API_KEY") or ""
	base_url = config.get("llm_base_url") or os.environ.get("FALLBACK_LLM_BASE_URL") or ""
	temperature = config.get("llm_temperature", 0.1)
	max_tokens = config.get("llm_max_tokens", 4096)

	# Cap max_tokens for Ollama - large values (e.g. 16384) contribute to OOM
	# on big models. Agent responses rarely exceed 2k tokens.
	if model.startswith("ollama/") and max_tokens > 4096:
		logger.info("Capping max_tokens from %d to 4096 for Ollama model", max_tokens)
		max_tokens = 4096

	llm_kwargs = {
		"model": model,
		"temperature": temperature,
		"max_tokens": max_tokens,
		"stream": True,
		# 300s default because 32B+ models on remote Ollama (no GPU on the
		# host) can easily take 2-3 min per call on the first cold request.
		# The previous 120s default was tuned for smaller local models and
		# is too tight for production-sized ones.
		"timeout": config.get("llm_timeout", 300),
	}

	if api_key:
		llm_kwargs["api_key"] = api_key
	if base_url:
		# Set both base_url and api_base - LiteLLM uses api_base for Ollama routing,
		# while base_url is used for OpenAI-compatible endpoints. Setting both ensures
		# it works for both local Ollama, remote Ollama, and custom API proxies.
		llm_kwargs["base_url"] = base_url
		llm_kwargs["api_base"] = base_url

	# Ollama-specific: limit context window to prevent OOM on large models.
	# llama3.3:70b defaults to 128k context which needs ~40GB+ just for KV cache.
	# 8192 is enough for agent tasks and keeps memory usage reasonable.
	num_ctx = int(config.get("llm_num_ctx") or 0)
	if num_ctx > 0:
		llm_kwargs["num_ctx"] = num_ctx
	elif model.startswith("ollama/"):
		llm_kwargs["num_ctx"] = 8192

	logger.info("Resolved LLM: model=%s, base_url=%s, temperature=%s, max_tokens=%s, num_ctx=%s",
		model, base_url or "(default)", temperature, max_tokens, llm_kwargs.get("num_ctx", "(default)"))
	return LLM(**llm_kwargs)


def _resolve_llm_for_tier(
	site_config: dict | None = None,
	tier: str | None = None,
) -> LLM:
	"""Resolve the LLM for a specific tier.

	If the tier has a model override in site_config (e.g. llm_model_agent),
	use that. Otherwise fall back to the default _resolve_llm().
	"""
	config = site_config or {}

	tier_model = ""
	tier_num_ctx = 0
	if tier:
		tier_model = (config.get(f"llm_model_{tier}") or "").strip()
		tier_num_ctx = int(config.get(f"llm_model_{tier}_num_ctx") or 0)

	if not tier_model:
		return _resolve_llm(config)

	# Build a tier-specific config by overriding model + num_ctx
	tier_config = dict(config)
	tier_config["llm_model"] = tier_model
	if tier_num_ctx > 0:
		tier_config["llm_num_ctx"] = tier_num_ctx
	logger.info("Resolved tier=%s LLM override: model=%s", tier, tier_model)
	return _resolve_llm(tier_config)


def _build_agent(
	role: str,
	goal: str,
	backstory: str,
	tools: list,
	llm: LLM,
) -> Agent:
	"""Create a single CrewAI Agent with standard configuration.

	All agents get:
	- allow_delegation=False (sequential process, no cross-agent delegation)
	- max_iter=2 (fail fast with slow local models instead of looping)
	- max_retry_limit=1 (LLM-level retry; default is 2 which burns 3 attempts
	  per agent when Ollama is dead - strict warmup catches that case, and
	  1 retry is enough to shake off a genuine transient error)
	- verbose=True (log agent reasoning)
	"""
	if not backstory:
		raise ValueError(f"Agent '{role}' has an empty backstory. Every agent must have a detailed system prompt.")

	return Agent(
		role=role,
		goal=goal,
		backstory=backstory,
		tools=tools,
		llm=llm,
		allow_delegation=False,
		max_iter=2,
		max_retry_limit=1,
		verbose=True,
	)


def build_agents(
	site_config: dict | None = None,
	custom_tools: dict | None = None,
) -> dict[str, Agent]:
	"""Build all 7 agents with the specified LLM configuration.

	Args:
		site_config: Per-connection settings (llm_model, llm_api_key, etc.)
			sent by the client app during WebSocket handshake.
		custom_tools: Optional dict mapping agent names to tool lists.
			Production path: build_mcp_tools(mcp_client) - live Frappe data.
			Falls back to the minimal local-only TOOL_ASSIGNMENTS when not passed
			(used by unit tests that don't have a WebSocket / MCP client).

	Returns:
		Dict mapping agent names to Agent instances:
		{
			"requirement": Agent(...),
			"assessment": Agent(...),
			"architect": Agent(...),
			"developer": Agent(...),
			"tester": Agent(...),
			"deployer": Agent(...),
			"orchestrator": Agent(...),
		}
	"""
	llm = _resolve_llm_for_tier(site_config, tier="agent")
	tools = custom_tools or TOOL_ASSIGNMENTS

	agents = {
		"requirement": _build_agent(
			role="Requirement Analyst",
			goal="Gather complete, unambiguous requirements from the user for Frappe customizations",
			backstory=backstories.REQUIREMENT_AGENT,
			tools=tools.get("requirement", []),
			llm=llm,
		),
		"assessment": _build_agent(
			role="Feasibility Assessor",
			goal="Verify permissions, detect conflicts, and assess whether the customization is safe to implement",
			backstory=backstories.ASSESSMENT_AGENT,
			tools=tools.get("assessment", []),
			llm=llm,
		),
		"architect": _build_agent(
			role="Solution Architect",
			goal="Design a complete technical solution using Frappe best practices based on requirements and assessment",
			backstory=backstories.ARCHITECT_AGENT,
			tools=tools.get("architect", []),
			llm=llm,
		),
		"developer": _build_agent(
			role="Frappe Developer",
			goal="Generate production-ready DocType definitions, Server Scripts, and Client Scripts following the Architect's design",
			backstory=backstories.DEVELOPER_AGENT,
			tools=tools.get("developer", []),
			llm=llm,
		),
		"tester": _build_agent(
			role="QA Validator",
			goal="Validate every item in the changeset against Frappe rules, check syntax, verify permissions, and detect naming conflicts",
			backstory=backstories.TESTER_AGENT,
			tools=tools.get("tester", []),
			llm=llm,
		),
		"deployer": _build_agent(
			role="Deployment Specialist",
			goal="Safely deploy approved changesets to the Frappe site with proper ordering, user approval, and rollback preparation",
			backstory=backstories.DEPLOYER_AGENT,
			tools=tools.get("deployer", []),
			llm=llm,
		),
		# Orchestrator is only needed for Process.hierarchical.
		# In sequential mode, tasks run in explicit order - no manager needed.
		# Kept commented for easy re-enabling if switching back to hierarchical.
		# "orchestrator": _build_agent(
		#     role="Orchestrator",
		#     goal="Route tasks to the right specialist agent...",
		#     backstory=backstories.ORCHESTRATOR_AGENT,
		#     tools=[], llm=llm,
		# ),
	}

	# Validate uniqueness of roles
	roles = [a.role for a in agents.values()]
	if len(roles) != len(set(roles)):
		raise ValueError(f"Duplicate agent roles detected: {roles}")

	logger.info("Built %d agents with LLM: %s", len(agents), llm.model)
	return agents


def check_llm_health(site_config: dict | None = None) -> dict:
	"""Check if the configured LLM backend is reachable.

	Returns:
		Dict with 'healthy' (bool), 'model' (str), and optional 'error' (str).
	"""
	try:
		llm = _resolve_llm(site_config)
		return {"healthy": True, "model": llm.model}
	except Exception as e:  # noqa: BLE001 — health-check boundary; _resolve_llm goes through litellm/crewai factory chain (3rd-party) and any raise must turn into a structured {healthy: False} result rather than propagate
		return {"healthy": False, "model": "unknown", "error": str(e)}
