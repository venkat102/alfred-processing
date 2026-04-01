"""CrewAI Crew + Task orchestration for the Alfred SDLC pipeline.

Builds a hierarchical crew with 6 SDLC tasks, an orchestrator manager agent,
state serialization to Redis, and human input handling via WebSocket.

Usage:
    from intern.agents.crew import build_intern_crew, run_crew

    crew = build_intern_crew("Create a DocType called Book", user_context, site_config)
    result = await run_crew(crew, store, site_id, conversation_id)
"""

import asyncio
import json
import logging
import time
from typing import Any

from crewai import Crew, Process, Task

from intern.agents.definitions import build_agents

logger = logging.getLogger("alfred.crew")

# ── Task Descriptions ────────────────────────────────────────────

TASK_DESCRIPTIONS = {
	"gather_requirements": {
		"description": (
			"Gather complete requirements from the user for their Frappe customization request.\n\n"
			"User's request: {prompt}\n"
			"User context: {user_context}\n\n"
			"Engage the user in a focused conversation to understand:\n"
			"1. What they want to build (DocType, workflow, report, etc.)\n"
			"2. What fields/data they need\n"
			"3. What business rules and validations apply\n"
			"4. What roles/permissions are needed\n"
			"5. Any constraints or dependencies\n\n"
			"Ask clarifying questions if anything is ambiguous. Do NOT proceed with vague requirements."
		),
		"expected_output": (
			"A structured requirement summary in JSON format:\n"
			"{\n"
			'  "objective": "one sentence summary",\n'
			'  "scope": "new_doctype | modify_existing | workflow | report | other",\n'
			'  "entities": [{"name": "...", "type": "DocType|Field|Workflow", "details": "..."}],\n'
			'  "business_rules": ["rule 1", "rule 2"],\n'
			'  "permissions": {"roles": ["Role1"], "operations": ["read", "write", "create"]},\n'
			'  "constraints": ["constraint 1"]\n'
			"}"
		),
	},
	"assess_feasibility": {
		"description": (
			"Assess the feasibility of the proposed customization.\n\n"
			"Requirements from previous phase: {requirements}\n\n"
			"You MUST:\n"
			"1. Use check_permission tool to verify user permissions for EVERY operation\n"
			"2. Use validate_name_available for any new DocTypes or documents\n"
			"3. Use has_active_workflow for any DocTypes that need workflows\n"
			"4. Check for naming conflicts with existing DocTypes\n"
			"5. Assess risk level\n\n"
			"NEVER guess permissions — ALWAYS use the check_permission tool."
		),
		"expected_output": (
			"A feasibility assessment in JSON format:\n"
			"{\n"
			'  "permissions_verified": true,\n'
			'  "permission_details": [{"doctype": "...", "action": "...", "permitted": true}],\n'
			'  "naming_conflicts": [],\n'
			'  "workflow_conflicts": [],\n'
			'  "risk_level": "low | medium | high",\n'
			'  "recommendation": "proceed | proceed_with_caution | block",\n'
			'  "reason": "..."\n'
			"}"
		),
	},
	"design_solution": {
		"description": (
			"Design a complete technical solution for the Frappe customization.\n\n"
			"Requirements: {requirements}\n"
			"Feasibility assessment: {assessment}\n\n"
			"Design the solution using Frappe best practices:\n"
			"1. DocType definitions with field types, naming rules, relationships\n"
			"2. Server Scripts with permission checks\n"
			"3. Client Scripts for UI enhancements\n"
			"4. Workflows if needed\n"
			"5. All new DocTypes go in the 'Alfred' module\n"
			"6. Follow Frappe naming conventions"
		),
		"expected_output": (
			"A technical design document in JSON format:\n"
			"{\n"
			'  "doctypes": [{"name": "...", "module": "Alfred", "fields": [...], "naming_rule": "..."}],\n'
			'  "server_scripts": [{"name": "...", "doctype": "...", "event": "...", "description": "..."}],\n'
			'  "client_scripts": [{"name": "...", "doctype": "...", "event": "...", "description": "..."}],\n'
			'  "workflows": [{"name": "...", "doctype": "...", "states": [...], "transitions": [...]}],\n'
			'  "custom_fields": [{"doctype": "...", "fields": [...]}]\n'
			"}"
		),
	},
	"generate_changeset": {
		"description": (
			"Generate production-ready Frappe document definitions and code.\n\n"
			"Technical design: {design}\n\n"
			"Generate complete, valid definitions for:\n"
			"1. DocType JSON definitions (every field property)\n"
			"2. Server Script Python code (with permission checks)\n"
			"3. Client Script JavaScript code\n"
			"4. Workflow JSON definitions\n"
			"5. Custom Field definitions\n\n"
			"CRITICAL: All Server Scripts MUST include permission checks.\n"
			"All DocTypes MUST use module='Alfred'."
		),
		"expected_output": (
			"A changeset as a JSON array:\n"
			'[{"op": "create", "doctype": "DocType", "data": {...complete definition...}},\n'
			' {"op": "create", "doctype": "Server Script", "data": {...}},\n'
			" ...]\n"
			"Each entry must be a complete, valid Frappe document definition."
		),
	},
	"validate_changeset": {
		"description": (
			"Validate the changeset before deployment.\n\n"
			"Changeset: {changeset}\n"
			"Original design: {design}\n\n"
			"Validate EVERY item:\n"
			"1. Check Python syntax of all Server Scripts\n"
			"2. Check JavaScript syntax of all Client Scripts\n"
			"3. Verify all Link field targets exist\n"
			"4. Verify no naming conflicts\n"
			"5. Verify workflow constraints\n"
			"6. Verify permission checks in Server Scripts\n"
			"7. Ensure changeset matches the design\n\n"
			"If ANY issue is found, report it with fix instructions. Do NOT fix it yourself."
		),
		"expected_output": (
			"A validation report in JSON format:\n"
			"{\n"
			'  "status": "PASS | FAIL",\n'
			'  "issues": [{"severity": "critical | warning", "item": "...", "issue": "...", "fix": "..."}],\n'
			'  "summary": "What was validated"\n'
			"}"
		),
	},
	"deploy_changeset": {
		"description": (
			"Deploy the validated changeset to the Frappe site.\n\n"
			"Changeset: {changeset}\n"
			"Validation report: {validation}\n\n"
			"1. Prepare deployment plan with correct ordering\n"
			"2. Check for data safety (use check_has_records)\n"
			"3. Present the plan to the user and request approval\n"
			"4. Execute deployment after approval\n"
			"5. Prepare rollback data\n\n"
			"NEVER deploy without user approval."
		),
		"expected_output": (
			"A deployment report in JSON format:\n"
			"{\n"
			'  "plan": [{"order": 1, "op": "create", "doctype": "...", "name": "..."}],\n'
			'  "approval": "approved | rejected",\n'
			'  "execution_log": [{"step": 1, "status": "success | failed", "details": "..."}],\n'
			'  "rollback_data": [...]\n'
			"}"
		),
	},
}


# ── Crew State Serialization ─────────────────────────────────────

class CrewState:
	"""Serializable state for crew resumption.

	Stores completed task outputs so the crew can be rebuilt
	and resumed from a checkpoint without re-running earlier tasks.
	"""

	def __init__(self):
		self.completed_tasks: dict[str, Any] = {}
		self.current_phase: str = "gather_requirements"
		self.delegation_count: int = 0
		self.max_delegations: int = 3
		self.started_at: float = time.time()
		self.last_updated: float = time.time()

	def mark_task_complete(self, task_name: str, output: Any):
		self.completed_tasks[task_name] = {
			"output": output if isinstance(output, str) else str(output),
			"completed_at": time.time(),
		}
		self.last_updated = time.time()

	def increment_delegation(self) -> bool:
		"""Increment delegation counter. Returns False if max exceeded."""
		self.delegation_count += 1
		self.last_updated = time.time()
		return self.delegation_count <= self.max_delegations

	def to_dict(self) -> dict:
		return {
			"completed_tasks": self.completed_tasks,
			"current_phase": self.current_phase,
			"delegation_count": self.delegation_count,
			"max_delegations": self.max_delegations,
			"started_at": self.started_at,
			"last_updated": self.last_updated,
		}

	@classmethod
	def from_dict(cls, data: dict) -> "CrewState":
		state = cls()
		state.completed_tasks = data.get("completed_tasks", {})
		state.current_phase = data.get("current_phase", "gather_requirements")
		state.delegation_count = data.get("delegation_count", 0)
		state.max_delegations = data.get("max_delegations", 3)
		state.started_at = data.get("started_at", time.time())
		state.last_updated = data.get("last_updated", time.time())
		return state


async def save_crew_state(store, site_id: str, conversation_id: str, state: CrewState):
	"""Persist crew state to Redis for resumption."""
	key = f"crew-state-{conversation_id}"
	try:
		await store.set_task_state(site_id, key, state.to_dict())
		logger.debug("Saved crew state for %s/%s", site_id, conversation_id)
	except Exception as e:
		logger.error("Failed to save crew state: %s (crew will continue but resume won't work)", e)


async def load_crew_state(store, site_id: str, conversation_id: str) -> CrewState | None:
	"""Load crew state from Redis for resumption."""
	key = f"crew-state-{conversation_id}"
	data = await store.get_task_state(site_id, key)
	if data is None:
		return None
	return CrewState.from_dict(data)


# ── Crew Builder ─────────────────────────────────────────────────

def build_intern_crew(
	user_prompt: str,
	user_context: dict | None = None,
	site_config: dict | None = None,
	previous_state: CrewState | None = None,
	custom_tools: dict | None = None,
) -> tuple[Crew, CrewState]:
	"""Build the Alfred SDLC crew with all tasks and agents.

	Args:
		user_prompt: The user's request/instruction.
		user_context: User context (email, roles, permissions).
		site_config: Site config (LLM settings, limits).
		previous_state: If resuming, the previous crew state.
		custom_tools: Override tool assignments (for real MCP tools).

	Returns:
		Tuple of (Crew instance, CrewState for tracking progress).
	"""
	user_context = user_context or {}
	site_config = site_config or {}
	state = previous_state or CrewState()
	max_retries = site_config.get("max_retries_per_agent", 3)
	state.max_delegations = max_retries

	# Build agents with the configured LLM
	agents = build_agents(site_config=site_config, custom_tools=custom_tools)

	# Build task context from previous outputs (for resumption)
	ctx = state.completed_tasks

	# Format task descriptions with available context
	format_vars = {
		"prompt": user_prompt,
		"user_context": json.dumps(user_context, indent=2),
		"requirements": ctx.get("gather_requirements", {}).get("output", "Not yet gathered"),
		"assessment": ctx.get("assess_feasibility", {}).get("output", "Not yet assessed"),
		"design": ctx.get("design_solution", {}).get("output", "Not yet designed"),
		"changeset": ctx.get("generate_changeset", {}).get("output", "Not yet generated"),
		"validation": ctx.get("validate_changeset", {}).get("output", "Not yet validated"),
	}

	# Create tasks — skip already completed ones on resume
	tasks = []
	task_map = {}

	task_definitions = [
		("gather_requirements", agents["requirement"], True),
		("assess_feasibility", agents["assessment"], False),
		("design_solution", agents["architect"], False),
		("generate_changeset", agents["developer"], False),
		("validate_changeset", agents["tester"], False),
		("deploy_changeset", agents["deployer"], True),
	]

	for task_name, agent, human_input in task_definitions:
		if task_name in state.completed_tasks:
			logger.info("Skipping completed task: %s", task_name)
			continue

		desc_template = TASK_DESCRIPTIONS[task_name]
		description = desc_template["description"].format(**format_vars)
		expected_output = desc_template["expected_output"]

		# Build context from earlier completed tasks
		context_tasks = [task_map[t] for t in task_map if t in state.completed_tasks or t in task_map]

		task = Task(
			description=description,
			expected_output=expected_output,
			agent=agent,
			human_input=human_input,
			context=context_tasks[-2:] if context_tasks else [],  # Last 2 tasks for context
		)
		tasks.append(task)
		task_map[task_name] = task

	# Build the crew with hierarchical process
	# Note: manager_agent must NOT be in the agents list (CrewAI requirement)
	specialist_agents = [a for name, a in agents.items() if name != "orchestrator"]
	crew = Crew(
		agents=specialist_agents,
		tasks=tasks,
		process=Process.hierarchical,
		manager_agent=agents["orchestrator"],
		memory=True,
		verbose=True,
		max_rpm=site_config.get("max_tasks_per_user_per_hour", 20),
	)

	return crew, state


# ── Crew Runner ──────────────────────────────────────────────────

async def run_crew(
	crew: Crew,
	state: CrewState,
	store=None,
	site_id: str = "",
	conversation_id: str = "",
	event_callback=None,
) -> dict:
	"""Run the crew and handle state persistence.

	Args:
		crew: The CrewAI Crew instance.
		state: The CrewState for tracking progress.
		store: Redis StateStore for persisting state (optional).
		site_id: Customer site ID for Redis namespace.
		conversation_id: Conversation ID for Redis key.
		event_callback: Async callback for pushing events (agent_started, etc.)

	Returns:
		Dict with final result, state, and execution log.
	"""
	async def notify(event_type: str, data: dict):
		if event_callback:
			await event_callback(event_type, data)

	await notify("crew_started", {"conversation_id": conversation_id, "tasks": len(crew.tasks)})

	try:
		# Run the crew in a thread pool (CrewAI is synchronous)
		loop = asyncio.get_event_loop()
		result = await loop.run_in_executor(None, crew.kickoff)

		# Update state with all completed tasks
		for i, task in enumerate(crew.tasks):
			task_names = [
				"gather_requirements", "assess_feasibility", "design_solution",
				"generate_changeset", "validate_changeset", "deploy_changeset",
			]
			if i < len(task_names) and task.output:
				state.mark_task_complete(task_names[i], task.output.raw if hasattr(task.output, "raw") else str(task.output))

		# Persist final state
		if store and site_id:
			await save_crew_state(store, site_id, conversation_id, state)

		await notify("crew_completed", {
			"conversation_id": conversation_id,
			"result": str(result),
		})

		return {
			"status": "completed",
			"result": str(result),
			"state": state.to_dict(),
		}

	except Exception as e:
		logger.error("Crew execution failed: %s", e, exc_info=True)

		# Persist state even on failure for debugging
		if store and site_id:
			await save_crew_state(store, site_id, conversation_id, state)

		await notify("crew_failed", {
			"conversation_id": conversation_id,
			"error": str(e),
		})

		return {
			"status": "failed",
			"error": str(e),
			"state": state.to_dict(),
		}
