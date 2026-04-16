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

from alfred.agents.condenser import make_condenser_callback
from alfred.agents.definitions import build_agents

# Pydantic output models (RequirementSpec, AssessmentResult,
# ArchitectureBlueprint, Changeset, DeploymentResult) live in
# `alfred.models.agent_outputs`. They're not imported here because
# CrewAI's `output_json` path fights with Ollama's code-fenced output
# (see the commented TODO near `task_definitions` below). When we flip
# Pydantic validation back on via a task callback, re-import them then.

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
			"Step 1: Identify which existing DocTypes are involved (verify via lookup_doctype)\n"
			"Step 2: Check if the requested functionality already exists (built-in notifications, workflows, fields)\n"
			"Step 3: Pick the smallest Frappe primitive. The decision tree (which primitive\n"
			"  for which user request shape) is part of the Frappe Knowledge Base and will be\n"
			"  auto-injected into the Developer's task; your RequirementSpec should name the\n"
			"  target primitive (Custom Field / Notification / Server Script / Workflow / DocType)\n"
			"  so the Developer can build against it.\n\n"
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
			"OUTPUT FORMAT (STRICT): Your entire Final Answer MUST be a single JSON array.\n"
			"Start with `[` and end with `]`. Do NOT include any prose, explanation, markdown\n"
			"headers (###), bullet points, or commentary before or after the JSON. Do NOT\n"
			"describe the schema - produce the changeset. If you only explain what exists,\n"
			"the task has FAILED.\n\n"
			"THINK FIRST, ACT SECOND (reasoning discipline - not in Final Answer):\n"
			"Before calling any tool, your very first Thought MUST begin with:\n"
			"  Target DocType: <the EXACT DocType name the Architect's design names>\n"
			"  PLAN:\n"
			"  1. create <doctype> '<name>' - <why, 1 line>\n"
			"  2. create <doctype> '<name>' - <why, 1 line>\n"
			"  (one line per item, maximum 6 items, no sub-bullets)\n"
			"Every `<doctype>` in the PLAN must either be the Target DocType from the line\n"
			"above or a Frappe meta-doctype (Notification, Server Script, Custom Field,\n"
			"Workflow, Client Script, Report). It MUST NOT be a different domain DocType\n"
			"just because a tool docstring or pattern template used one as an example.\n"
			"Then, for each item in your plan, call `lookup_doctype(<Target DocType>, layer='framework')`\n"
			"once to get the authoritative field list, and `lookup_pattern(<pattern name>,\n"
			"kind='name')` if a matching curated template exists. Only after all lookups are\n"
			"done do you emit the Final Answer.\n"
			"The plan stays in Thought: - it never leaks into Final Answer. Final Answer is\n"
			"raw JSON only. The plan is discipline for you, not output for the parser.\n\n"
			"TASK: Generate production-ready Frappe document definitions that can be directly\n"
			"inserted via frappe.get_doc(data).insert().\n\n"
			"Technical design: {design}\n\n"
			"For each item, produce the COMPLETE document definition with ALL required fields.\n\n"
			"SHAPE OF EACH ITEM (placeholders in <angle brackets> - substitute them with values\n"
			"from the design, NOT from these examples):\n\n"
			"Notification:\n"
			'  {{"op": "create", "doctype": "Notification", "data": {{\n'
			'    "doctype": "Notification",\n'
			'    "name": "<short descriptive name>",\n'
			'    "subject": "<subject line, may use {{{{ doc.<field> }}}} Jinja>",\n'
			'    "document_type": "<exact DocType name from the design>",\n'
			'    "event": "<New | Save | Submit | Cancel | Value Change | Days Before | Days After>",\n'
			'    "channel": "Email",\n'
			'    "recipients": [{{"receiver_by_document_field": "<link field holding the user>"}}],\n'
			'    "message": "<HTML/Jinja body>",\n'
			'    "enabled": 1\n'
			"  }}}}\n\n"
			"Server Script:\n"
			'  {{"op": "create", "doctype": "Server Script", "data": {{\n'
			'    "doctype": "Server Script",\n'
			'    "name": "<short descriptive name>",\n'
			'    "script_type": "DocType Event",\n'
			'    "reference_doctype": "<exact DocType name>",\n'
			'    "doctype_event": "<Before Insert | After Insert | Before Save | After Save | ...>",\n'
			'    "script": "<python body following the FKB rules (pre-bound names, no imports)>"\n'
			"  }}}}\n\n"
			"Custom Field:\n"
			'  {{"op": "create", "doctype": "Custom Field", "data": {{\n'
			'    "doctype": "Custom Field",\n'
			'    "dt": "<target DocType>",\n'
			'    "fieldname": "<snake_case>",\n'
			'    "label": "<Human Label>",\n'
			'    "fieldtype": "<Data | Link | Select | Int | ...>"\n'
			"  }}}}\n\n"
			"CRITICAL RULES:\n"
			"- Every 'data' object MUST include 'doctype' matching the outer doctype\n"
			"- Include ALL mandatory fields for the document type\n"
			"- Mandatory field lists come from `lookup_doctype(name, layer='framework')` - verify\n"
			"  against the live framework schema, do NOT recall from memory\n"
			"- Use `lookup_doctype` to verify field names against the live site before writing\n"
			"  the changeset, but DO NOT narrate what you found - use it internally\n"
			"- Use `lookup_pattern` to retrieve canonical templates for common customization\n"
			"  idioms (approval notification, validation script, audit log, etc.) - adapt the\n"
			"  template rather than reinventing the pattern from scratch. Pattern templates\n"
			"  carry their own event-selection and recipient rules; follow them.\n"
			"- STAY IN THE USER'S DOMAIN: use the DocType names and field names from the design.\n"
			"  Never substitute a different DocType even if it would be easier to describe.\n\n"
			"FINAL ANSWER FORMAT: Raw JSON array only. No backticks, no code fences, no prose.\n"
			'Start with `[{{` and end with `}}]`.'
		),
		"expected_output": (
			'[{"op": "create", "doctype": "<TYPE>", "data": {"doctype": "<TYPE>", "name": "<NAME>", ...all mandatory fields for that TYPE...}}]'
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
	# ── USER REQUEST (FRONTLOADED - this is the first thing the model reads) ──
	"====== USER REQUEST ======\n"
	"{prompt}\n"
	"==========================\n\n"
	"That is the ONLY request you are working on. Every decision you make\n"
	"must come from that text above. Do NOT pick a DocType from your training\n"
	"data or from examples in docstrings - use the DocType named in the USER\n"
	"REQUEST above.\n\n"
	# ── HOW TO ACT ──
	"TASK: In one pass, produce a deployable Frappe changeset for the request above.\n"
	"You are a Frappe Developer agent. You have MCP tools to query the live site.\n"
	"You must call tools BEFORE emitting a Final Answer - ungrounded answers are rejected.\n\n"
	"STEP 1 - Identify the target DocType from the USER REQUEST above. Write it down as\n"
	"your first Thought in this exact format:\n"
	"  Target DocType: <exact DocType name from user request>\n"
	"  Task type: validation / notification / custom_field / workflow / new_doctype / other\n"
	"  Plan: <one sentence>\n\n"
	"STEP 2 - Call lookup_doctype(<Target DocType>, layer=\"framework\") ONCE to get the\n"
	"real field list. Substitute <Target DocType> with what you wrote in STEP 1.\n\n"
	"STEP 3 - If task type is validation, also call\n"
	"lookup_pattern(\"validation_server_script\", kind=\"name\") for the canonical template.\n"
	"If task type is notification, call lookup_pattern(\"approval_notification\", kind=\"name\").\n"
	"Adapt the template to the Target DocType.\n\n"
	"STEP 4 - Output the changeset as a JSON array. Raw JSON only, no prose.\n\n"
	# Platform rules, API reference, and house style are auto-injected from
	# the Frappe Knowledge Base into the banner prepended to this task turn
	# (look above the USER REQUEST block). That banner covers: which
	# primitive to pick for the user's request, Server Script sandbox
	# constraints (no `import`), naming conventions, error-handling style.
	# Read it and follow it - do NOT re-derive the rules here.
	#
	# Short regression-protection reminder for the MOST COMMON failure
	# mode: routing a validation request to a Notification. The KB entry
	# `minimal_change_principle` covers this in depth; the stub here is
	# belt-and-braces so even without auto-inject the Lite agent gets it.
	"MINIMAL-CHANGE REMINDER:\n"
	"- validate / restrict / reject / throw / block / prevent / require\n"
	"  on an existing DocType -> Server Script (NEVER a Notification,\n"
	"  NEVER a new DocType). See the auto-injected KB banner for the\n"
	"  full decision tree and shape.\n\n"
	"OUTPUT FORMAT (STRICT):\n"
	"- Final Answer is a RAW JSON ARRAY. Starts with `[`, ends with `]`.\n"
	"- No prose, no markdown, no headers, no code fences before or after.\n"
	"- Do not describe the DocType you looked up - produce the changeset.\n"
	"- If your Final Answer starts with 'The provided JSON structure' or 'Here is a\n"
	"  breakdown' or any similar documentation phrasing, YOU HAVE FAILED.\n"
	"- If your Final Answer mentions a DocType that the USER REQUEST above did not\n"
	"  name (e.g. Sales Order when the user said Employee), YOU HAVE FAILED.\n\n"
	"User context: {user_context}\n\n"
	"Remember: the USER REQUEST at the top of this task is the only source of truth.\n"
	"Re-read it before writing your Final Answer."
)

LITE_TASK_EXPECTED_OUTPUT = (
	'[{"op": "create", "doctype": "Notification", "data": {"doctype": "Notification", "name": "...", "subject": "...", "document_type": "...", "event": "...", "channel": "...", "recipients": [...], "message": "..."}}]'
)


# ── Insights Pipeline (single-agent, read-only, markdown output) ─
#
# Phase B of the three-mode chat feature. One agent with read-only MCP tools
# answers questions about the user's current Frappe site. Never produces a
# changeset, never writes to the DB.
#
# Task is deliberately named "generate_insights_reply" so the pipeline can
# route output to an `insights_reply` message type rather than the dev-mode
# `changeset` message.

INSIGHTS_TASK_DESCRIPTION = (
	"You are answering a user's question about their current Frappe/ERPNext site.\n"
	"This is READ-ONLY Insights mode. You must NOT produce a changeset, JSON,\n"
	"code, or any build artefact. You produce a concise, factual answer in\n"
	"plain markdown that the user can read in a chat UI.\n\n"
	"User question: {prompt}\n"
	"User context: {user_context}\n\n"
	"HOW TO ANSWER:\n"
	"1. Read the question carefully. Identify exactly what site information the\n"
	"   user needs.\n"
	"2. Use the read-only MCP tools available to you to gather the facts. You\n"
	"   have access to: lookup_doctype, lookup_pattern, get_site_info,\n"
	"   get_doctypes, get_existing_customizations, get_user_context,\n"
	"   check_permission, has_active_workflow, check_has_records,\n"
	"   validate_name_available. Prefer `lookup_doctype` with `layer=\"site\"`\n"
	"   or `layer=\"both\"` over `get_doctype_schema`.\n"
	"3. Budget your tool calls - you have a hard cap of 5 calls per turn. If you\n"
	"   cannot get the full answer in 5 calls, say what you found and what's\n"
	"   missing.\n"
	"4. Ground every factual claim in a tool response. If you cannot verify a\n"
	"   fact with a tool, say so explicitly - never guess about the user's site.\n"
	"5. If the user's question cannot be answered with read-only tools (e.g.\n"
	"   they are asking you to build something), respond with a short note\n"
	"   suggesting they rephrase as a build request.\n\n"
	"OUTPUT FORMAT (STRICT):\n"
	"- Plain markdown, 2-8 sentences for most questions.\n"
	"- Use markdown lists when enumerating (e.g. 'You have these workflows: ...').\n"
	"- Do NOT output JSON, code fences around the whole answer, or any build\n"
	"  artefact.\n"
	"- Do NOT narrate your tool calls or internal reasoning - just the answer.\n"
	"- Do NOT end with 'Would you like me to...' unless you have a concrete\n"
	"  build suggestion that the user seemed to be leading toward.\n"
)

INSIGHTS_TASK_EXPECTED_OUTPUT = (
	"A short markdown answer (2-8 sentences) that directly answers the user's "
	"question using facts gathered from read-only MCP tools. No JSON, no code, "
	"no changeset."
)


def build_insights_crew(
	user_prompt: str,
	user_context: dict | None = None,
	site_config: dict | None = None,
	insights_tools: list | None = None,
) -> tuple[Crew, CrewState]:
	"""Build the single-agent Insights crew.

	Same shape as `build_lite_crew` - returns (crew, state) - but the agent
	has a different role, a markdown-output task (not JSON), and is restricted
	to read-only MCP tools. The task name is `generate_insights_reply` so the
	pipeline's post-processing can route the output to an `insights_reply`
	message type.

	Args:
		user_prompt: The user's raw question.
		user_context: User roles/permissions (threaded into the task description).
		site_config: LLM configuration from Alfred Settings.
		insights_tools: CrewAI @tool list from `build_mcp_tools(...)["insights"]`.
	"""
	from alfred.agents import backstories
	from alfred.agents.definitions import _resolve_llm

	user_context = user_context or {}
	site_config = site_config or {}
	state = CrewState()

	llm = _resolve_llm(site_config)

	# Role deliberately distinct from "Frappe Developer" so the UI can badge
	# this as Insights mode. The backstory is site-information-focused.
	backstory = getattr(backstories, "INSIGHTS_AGENT", None) or (
		"You are a Frappe/ERPNext site information specialist. Users ask you "
		"questions about their current site - what DocTypes exist, which "
		"workflows are active, what permissions they have, what customizations "
		"are installed. You answer with facts gathered from read-only MCP "
		"tools, never guessing, never building anything. You are concise and "
		"helpful, and if a question cannot be answered from site state alone "
		"you say so directly."
	)

	insights_agent = Agent(
		role="Frappe Site Information Specialist",
		goal="Answer the user's question about their Frappe site using read-only site data",
		backstory=backstory,
		tools=insights_tools or [],
		llm=llm,
		allow_delegation=False,
		max_iter=4,
		verbose=True,
	)

	description = INSIGHTS_TASK_DESCRIPTION.format(
		prompt=user_prompt,
		user_context=json.dumps(user_context, indent=2),
	)

	task = Task(
		description=description,
		expected_output=INSIGHTS_TASK_EXPECTED_OUTPUT,
		agent=insights_agent,
		human_input=False,
	)

	crew = Crew(
		agents=[insights_agent],
		tasks=[task],
		process=Process.sequential,
		memory=False,
		verbose=True,
		max_rpm=site_config.get("max_tasks_per_user_per_hour", 20),
	)
	# Named distinctly so the pipeline can tell this output apart from a
	# dev-mode changeset run.
	crew._alfred_task_names = ["generate_insights_reply"]

	return crew, state


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

		# Phase 2: condense upstream task outputs in place so the next
		# task's context aggregation reads a compact form instead of
		# the full verbose output. Returns None for tasks we must not
		# condense (generate_changeset is the final artifact).
		condenser = make_condenser_callback(task_name)
		if condenser is not None:
			task_kwargs["callback"] = condenser

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
		# When resuming a partial run, crew.tasks only contains the tasks that
		# still need to execute - their indices don't line up with task_names.
		# `_alfred_task_names` is attached by build_alfred_crew and lists the
		# actual task names in execution order, so we prefer it when present.
		active_task_names = getattr(crew, "_alfred_task_names", task_names)

		changeset_output = None
		for i, task in enumerate(crew.tasks):
			if i < len(task_names) and task.output:
				raw = task.output.raw if hasattr(task.output, "raw") else str(task.output)
				state.mark_task_complete(task_names[i], raw)
				# The developer's output (generate_changeset) has the full document
				# definitions. Use it as the changeset instead of the deployer's
				# plan summary.
				actual_name = (
					active_task_names[i] if i < len(active_task_names) else task_names[i]
				)
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
