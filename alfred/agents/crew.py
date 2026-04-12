"""CrewAI Crew + Task orchestration for the Alfred SDLC pipeline.

Builds a hierarchical crew with 6 SDLC tasks, an orchestrator manager agent,
state serialization to Redis, and human input handling via WebSocket.

Usage:
    from alfred.agents.crew import build_alfred_crew, run_crew

    crew = build_alfred_crew("Create a DocType called Book", user_context, site_config)
    result = await run_crew(crew, store, site_id, conversation_id)
"""

import asyncio
import json
import logging
import time
from typing import Any

from crewai import Agent, Crew, Process, Task

from alfred.agents.definitions import build_agents
from alfred.models.agent_outputs import (
	RequirementSpec,
	AssessmentResult,
	ArchitectureBlueprint,
	Changeset,
	DeploymentResult,
)

logger = logging.getLogger("alfred.crew")

# ── Task Descriptions ────────────────────────────────────────────

TASK_DESCRIPTIONS = {
	"gather_requirements": {
		"description": (
			"Gather complete requirements from the user for their Frappe customization request.\n\n"
			"User's request: {prompt}\n"
			"User context: {user_context}\n\n"
			"CRITICAL: Before proposing ANY new creation, identify what ALREADY EXISTS in Frappe/ERPNext.\n"
			"Frappe and its apps (ERPNext, HRMS, Education, etc.) have hundreds of built-in DocTypes,\n"
			"fields, workflows, and notifications. Your job is to find the MINIMAL change needed.\n\n"
			"Step 1: Identify which existing DocTypes are involved (e.g., Expense Claim, Leave Application)\n"
			"Step 2: Check if the requested functionality already exists (built-in notifications, workflows, fields)\n"
			"Step 3: Determine the SMALLEST customization needed:\n"
			"  - Need an email alert? → Use the built-in Notification DocType\n"
			"  - Need a new field? → Add a Custom Field to the existing DocType\n"
			"  - Need custom logic? → Add a Server Script on the existing DocType\n"
			"  - Need a truly new entity? → Only THEN create a new DocType\n\n"
			"Do NOT create new DocTypes for things that already exist.\n"
			"Do NOT create Server Scripts when a Notification DocType would suffice.\n\n"
			"Ask clarifying questions if anything is ambiguous."
		),
		"expected_output": (
			"A structured RequirementSpec in JSON format:\n"
			"{\n"
			'  "summary": "Brief description of what is being built",\n'
			'  "customizations_needed": [\n'
			'    {"type": "DocType|Custom Field|Server Script|Client Script|Workflow|Report|Notification|Print Format",\n'
			'     "name": "Proposed name", "description": "What this does",\n'
			'     "fields": [...], "needs_workflow": false, "needs_server_script": false}\n'
			"  ],\n"
			'  "dependencies": ["Existing DocTypes this depends on"],\n'
			'  "open_questions": ["Any remaining ambiguities"]\n'
			"}"
		),
	},
	"assess_feasibility": {
		"description": (
			"Assess the feasibility of the proposed customization.\n\n"
			"Requirements from previous phase: {requirements}\n\n"
			"CRITICAL VALIDATION STEPS:\n"
			"1. Verify that the requirements DON'T duplicate existing functionality.\n"
			"   - If the requirement says 'create DocType X' but X already exists → flag it.\n"
			"   - If a Server Script is proposed but a Notification would suffice → flag it.\n"
			"2. Use check_permission tool to verify user permissions for EVERY operation\n"
			"3. Use validate_name_available for any new DocTypes or documents\n"
			"4. Use has_active_workflow for any DocTypes that need workflows\n"
			"5. Check for naming conflicts with existing DocTypes\n"
			"6. Assess risk level\n\n"
			"If the requirements propose creating something that already exists in Frappe/ERPNext,\n"
			"set recommendation to 'proceed_with_caution' and explain what already exists.\n\n"
			"NEVER guess permissions - ALWAYS use the check_permission tool."
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
			"Generate production-ready Frappe document definitions that can be directly inserted via frappe.get_doc(data).insert().\n\n"
			"Technical design: {design}\n\n"
			"For each item, produce the COMPLETE document definition with ALL required fields.\n\n"
			"Examples of complete definitions:\n\n"
			"Notification document:\n"
			'  {{"op": "create", "doctype": "Notification", "data": {{\n'
			'    "doctype": "Notification",\n'
			'    "name": "Notify Expense Approver",\n'
			'    "subject": "New Expense Claim {{{{ doc.name }}}} from {{{{ doc.employee_name }}}}",\n'
			'    "document_type": "Expense Claim",\n'
			'    "event": "New",\n'
			'    "channel": "Email",\n'
			'    "recipients": [{{"receiver_by_document_field": "expense_approver"}}],\n'
			'    "message": "<p>A new expense claim {{{{ doc.name }}}} has been submitted.</p>",\n'
			'    "enabled": 1\n'
			"  }}}}\n\n"
			"Server Script document:\n"
			'  {{"op": "create", "doctype": "Server Script", "data": {{\n'
			'    "doctype": "Server Script",\n'
			'    "name": "Validate Expense Claim",\n'
			'    "script_type": "DocType Event",\n'
			'    "reference_doctype": "Expense Claim",\n'
			'    "doctype_event": "Before Save",\n'
			'    "script": "if not doc.expense_approver:\\n    frappe.throw(\\"Expense Approver is required\\")"\n'
			"  }}}}\n\n"
			"CRITICAL RULES:\n"
			"- Every 'data' object MUST include 'doctype' matching the outer doctype\n"
			"- Include ALL mandatory fields for the document type\n"
			"- For Notifications: subject, document_type, event, channel, recipients, message are required\n"
			"- For Server Scripts: script_type, reference_doctype, doctype_event, script are required\n"
			"- For Custom Fields: dt (target doctype), fieldname, fieldtype, label are required\n"
		),
		"expected_output": (
			"A changeset as a JSON array:\n"
			'[{"op": "create", "doctype": "Notification", "data": {...COMPLETE document with ALL fields...}}]\n'
			"Each entry must be a complete, valid Frappe document definition that can be inserted directly."
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
		# Tracks pre-preview dry-run self-heal retries. Capped at 1 to prevent
		# infinite "fix the fix" loops.
		self.dry_run_retries: int = 0

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
			"dry_run_retries": self.dry_run_retries,
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
		state.dry_run_retries = data.get("dry_run_retries", 0)
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


# ── Lite Pipeline (single-agent, single-task) ───────────────────
#
# Used for lower-tier plans: one agent handles requirements + design + codegen
# in a single pass. ~5× cheaper and ~5× faster than the full 6-agent crew at
# the cost of cross-agent validation. The pre-preview dry-run + approve-time
# safety net still apply downstream, so broken changesets are still caught.
#
# The task is deliberately named "generate_changeset" so that run_crew's
# existing changeset extraction and phase_map lookup work without modification.

LITE_TASK_DESCRIPTION = (
	"Complete the entire Frappe customization in one pass: understand the request, "
	"verify DocType schemas against the LIVE site, design the minimal change, and "
	"generate a deployable changeset.\n\n"
	"User request: {prompt}\n"
	"User context: {user_context}\n\n"
	"MANDATORY STEPS (in order):\n"
	"1. Call get_doctype_schema on EVERY DocType you reference to get the real field names.\n"
	"2. Call check_permission for the operations you plan to perform.\n"
	"3. Call get_existing_customizations to avoid duplicating already-installed items.\n"
	"4. Apply the minimal change principle:\n"
	"   - Email alert? → Notification DocType\n"
	"   - New field? → Custom Field\n"
	"   - Custom logic? → Server Script (with permission check)\n"
	"   - New entity? → Only THEN create a new DocType\n"
	"5. Produce a JSON array of complete document definitions.\n\n"
	"Every data object must be directly deployable via frappe.get_doc(data).insert(). "
	"Downstream dry-run validation will reject any item with missing mandatory fields, "
	"so be thorough in this single pass - there is no Tester agent to catch mistakes."
)

LITE_TASK_EXPECTED_OUTPUT = (
	"A JSON array of complete Frappe document definitions:\n"
	'[{"op": "create", "doctype": "...", "data": {...COMPLETE document with ALL fields...}}]\n'
	"Every data object must include 'doctype' matching the outer doctype plus all "
	"mandatory fields for that document type."
)


def build_lite_crew(
	user_prompt: str,
	user_context: dict | None = None,
	site_config: dict | None = None,
	previous_state: CrewState | None = None,
	lite_tools: list | None = None,
) -> tuple[Crew, CrewState]:
	"""Build the single-agent lite pipeline.

	Returns the same (crew, state) tuple as build_alfred_crew so the rest of the
	pipeline - run_crew, _extract_changes, _dry_run_with_retry, preview delivery -
	works unchanged.

	Args:
		user_prompt: The user's (already-enhanced) request.
		user_context: User roles/permissions.
		site_config: Per-connection settings (LLM config, limits).
		previous_state: If resuming, the previous CrewState. Lite pipeline has
			a single task so resumption is effectively no-op.
		lite_tools: List of CrewAI @tool objects to give the lite agent. In
			production this is build_mcp_tools(mcp_client)["lite"].
	"""
	from alfred.agents import backstories
	from alfred.agents.definitions import _resolve_llm

	user_context = user_context or {}
	site_config = site_config or {}
	state = previous_state or CrewState()

	llm = _resolve_llm(site_config)

	# Role deliberately matches the full-mode Developer so the UI's
	# AGENT_PHASE_MAP and run_crew's phase_map both resolve correctly without
	# any lite-specific branching. The only visible distinguisher in the UI is
	# the "Basic Mode" badge, driven by site_config.pipeline_mode.
	lite_agent = Agent(
		role="Frappe Developer",
		goal="Produce a complete, deployable Frappe changeset from a user request in a single pass",
		backstory=backstories.LITE_AGENT,
		tools=lite_tools or [],
		llm=llm,
		allow_delegation=False,
		max_iter=4,  # Higher than full-mode agents since this one does everything
		verbose=True,
	)

	description = LITE_TASK_DESCRIPTION.format(
		prompt=user_prompt,
		user_context=json.dumps(user_context, indent=2),
	)

	task = Task(
		description=description,
		expected_output=LITE_TASK_EXPECTED_OUTPUT,
		agent=lite_agent,
		human_input=False,
	)

	crew = Crew(
		agents=[lite_agent],
		tasks=[task],
		process=Process.sequential,
		memory=False,
		verbose=True,
		max_rpm=site_config.get("max_tasks_per_user_per_hour", 20),
	)
	# Named to match what run_crew looks for as the changeset source.
	crew._alfred_task_names = ["generate_changeset"]

	return crew, state


# ── Crew Builder ─────────────────────────────────────────────────

def build_alfred_crew(
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

	# Create tasks - skip already completed ones on resume
	tasks = []
	task_map = {}

	# Pydantic output models - kept for reference and downstream parsing,
	# but NOT wired into CrewAI's output_json because local models (Ollama)
	# wrap JSON in markdown code fences (```json ... ```) which CrewAI's
	# TaskOutput parser can't handle (pydantic ValidationError on json_dict).
	# TODO: Re-enable output_json when using models that reliably produce raw JSON,
	#       or when CrewAI adds code-fence stripping to its output parser.
	# output_models = {
	#     "gather_requirements": RequirementSpec,
	#     "assess_feasibility": AssessmentResult,
	#     "design_solution": ArchitectureBlueprint,
	#     "generate_changeset": Changeset,
	#     "deploy_changeset": DeploymentResult,
	# }

	# human_input=False for all tasks: the prompt is already enhanced before reaching
	# agents, and deployment approval comes through the changeset preview UI, not
	# through CrewAI's stdin-based input() which blocks the thread and has no UI wiring.
	# TODO: Re-enable human_input when WebSocket-based input handler is wired through.
	task_definitions = [
		("gather_requirements", agents["requirement"], False),
		("assess_feasibility", agents["assessment"], False),
		("design_solution", agents["architect"], False),
		("generate_changeset", agents["developer"], False),
		("validate_changeset", agents["tester"], False),
		("deploy_changeset", agents["deployer"], False),
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

		task_kwargs = {
			"description": description,
			"expected_output": expected_output,
			"agent": agent,
			"human_input": human_input,
			"context": context_tasks[-2:] if context_tasks else [],
		}

		task = Task(**task_kwargs)
		tasks.append(task)
		task_map[task_name] = task

	# Sequential process: tasks run in explicit order, no manager agent overhead.
	# Hierarchical was causing long delegation loops with local models (Ollama).
	specialist_agents = [a for name, a in agents.items() if name != "orchestrator"]
	crew = Crew(
		agents=specialist_agents,
		tasks=tasks,
		process=Process.sequential,
		memory=False,  # Disabled - agents get context via task chaining, not vector memory
		verbose=True,
		max_rpm=site_config.get("max_tasks_per_user_per_hour", 20),
		max_iter=10,
	)
	# Attach task names so run_crew can send per-phase WebSocket events
	crew._alfred_task_names = [t[0] for t in task_definitions if t[0] not in state.completed_tasks]

	return crew, state


# ── Crew Runner ──────────────────────────────────────────────────

async def run_crew(
	crew: Crew,
	state: CrewState,
	store=None,
	site_id: str = "",
	conversation_id: str = "",
	event_callback=None,
	human_input_handler=None,
) -> dict:
	"""Run the crew and handle state persistence.

	Args:
		crew: The CrewAI Crew instance.
		state: The CrewState for tracking progress.
		store: Redis StateStore for persisting state (optional).
		site_id: Customer site ID for Redis namespace.
		conversation_id: Conversation ID for Redis key.
		event_callback: Async callback for pushing events (agent_started, etc.)
		human_input_handler: Async callable(question: str) -> str for user input.
			If provided, overrides CrewAI's default stdin-based human_input.

	Returns:
		Dict with final result, state, and execution log.
	"""
	async def notify(event_type: str, data: dict):
		if event_callback:
			await event_callback(event_type, data)

	await notify("crew_started", {"conversation_id": conversation_id, "tasks": len(crew.tasks)})

	# Override CrewAI's human_input if a handler is provided.
	# CrewAI's Task.human_input triggers a call to the built-in input() function.
	# We monkey-patch it to route through our WebSocket handler instead.
	original_builtin_input = None
	if human_input_handler:
		import builtins
		original_builtin_input = builtins.input

		def _ws_input(prompt_text=""):
			"""Replacement for builtins.input() that routes through WebSocket."""
			import concurrent.futures
			loop = asyncio.new_event_loop()
			try:
				return loop.run_until_complete(human_input_handler(prompt_text))
			finally:
				loop.close()

		builtins.input = _ws_input

	try:
		# Send per-phase start events before the crew runs.
		# Since crew.kickoff() is synchronous (runs in executor), we send all
		# the phase names upfront so the UI knows what to expect.
		task_names_list = getattr(crew, "_alfred_task_names", [])
		phase_map = {
			"gather_requirements": ("Requirement Analyst", "requirement"),
			"assess_feasibility": ("Feasibility Assessor", "assessment"),
			"design_solution": ("Solution Architect", "architecture"),
			"generate_changeset": ("Frappe Developer", "development"),
			"validate_changeset": ("QA Validator", "testing"),
			"deploy_changeset": ("Deployment Specialist", "deployment"),
		}

		# Notify each phase start, run the crew, then notify completed.
		# We can't send mid-execution (crew is sync in a thread), so we send
		# the first phase start now, and the rest after completion.
		if task_names_list:
			first_agent, first_phase = phase_map.get(task_names_list[0], ("Agent", ""))
			await notify("task_started", {"agent": first_agent, "phase": first_phase, "status": "started"})

		# Run the crew in a thread pool (CrewAI is synchronous)
		loop = asyncio.get_event_loop()
		result = await loop.run_in_executor(None, crew.kickoff)

		# Update state with all completed tasks and find the developer's changeset
		task_names = [
			"gather_requirements", "assess_feasibility", "design_solution",
			"generate_changeset", "validate_changeset", "deploy_changeset",
		]
		changeset_output = None
		for i, task in enumerate(crew.tasks):
			if i < len(task_names) and task.output:
				raw = task.output.raw if hasattr(task.output, "raw") else str(task.output)
				state.mark_task_complete(task_names[i], raw)
				# The developer's output (generate_changeset) has the full document definitions.
				# Use it as the changeset instead of the deployer's plan summary.
				actual_name = getattr(crew, "_alfred_task_names", task_names)[i] if i < len(getattr(crew, "_alfred_task_names", [])) else task_names[i]
				if actual_name == "generate_changeset":
					changeset_output = raw

		# Persist final state
		if store and site_id:
			await save_crew_state(store, site_id, conversation_id, state)

		# Use the developer's changeset output if available, otherwise fall back to final result
		final_result = changeset_output or str(result)

		await notify("crew_completed", {
			"conversation_id": conversation_id,
			"result": final_result,
		})

		return {
			"status": "completed",
			"result": final_result,
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

	finally:
		# Restore original input() if we monkey-patched it
		if original_builtin_input is not None:
			import builtins
			builtins.input = original_builtin_input
