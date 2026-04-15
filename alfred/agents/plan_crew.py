"""Plan crew builder - Phase C of the three-mode chat feature.

Plan mode runs a 3-agent planning crew (Requirement Analyst,
Feasibility Assessor, Solution Architect) that stops BEFORE code
generation and produces a structured plan document instead of a
deployable changeset.

Shape and conventions:
  - Reuses the existing agent definitions from `alfred.agents.definitions`
    so the Plan and Dev pipelines read from the same source of truth -
    same roles, same backstories, same MCP tool bindings per role.
  - The final task is named `generate_plan_doc` (NOT `generate_changeset`)
    so downstream code (`_phase_post_crew` for Dev, `_run_plan_short_circuit`
    for Plan) can distinguish the two output shapes.
  - Uses the same handoff condenser callbacks as the full crew for the
    first two tasks. The final task is NOT condensed because its output
    is the artifact we want to keep verbatim.

The plan doc is a user-facing summary, not an implementation spec. It's
meant to be readable in a chat panel, not consumed by another agent.
When the user approves the plan, the next turn runs Dev mode with the
plan doc injected into the enhanced prompt as a "CONTEXT: approved plan"
block - see `alfred.api.pipeline._phase_enhance` for the handoff.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from crewai import Crew, Process, Task

from alfred.agents.condenser import make_condenser_callback

if TYPE_CHECKING:
	from alfred.agents.crew import CrewState

logger = logging.getLogger("alfred.plan_crew")


# ── Task descriptions for Plan mode ──────────────────────────────────
#
# The Requirement and Assessment tasks share their template with the
# Dev pipeline (they produce the same reasoning artefacts - a
# RequirementSpec and an AssessmentResult). We pull those in from the
# existing TASK_DESCRIPTIONS dict so they stay in sync.
#
# The final `generate_plan_doc` task is unique to Plan mode.

_PLAN_DOC_TASK_DESCRIPTION = """\
You are finalising the plan document that the user will see in the chat.
The user has NOT approved anything yet - your job is to produce a clear,
concise plan they can review, refine, or approve.

USER REQUEST: {prompt}

USER CONTEXT: {user_context}

INPUTS FROM EARLIER AGENTS:
- Requirements (from the Requirement Analyst):
  {requirements}
- Feasibility + permission check (from the Feasibility Assessor):
  {assessment}
- Architectural design (from the Solution Architect):
  {design}

OUTPUT FORMAT (STRICT): Your entire Final Answer MUST be a single JSON
object matching this schema exactly. No prose before or after. No code
fences. Use `null` for missing optional fields.

{{
  "title": "<short title, 5-10 words>",
  "summary": "<one paragraph explaining what will be built and why, 2-3 sentences>",
  "steps": [
    {{
      "order": 1,
      "action": "<one-line description of the concrete action, e.g. 'Create Notification 'Expense Claim Approval' on Expense Claim'>",
      "rationale": "<one sentence explaining why this step is needed>",
      "doctype": "<primary Frappe doctype this step touches, or null>"
    }},
    ...
  ],
  "doctypes_touched": ["<doctype name>", ...],
  "risks": [
    "<risk 1 - one sentence>",
    "<risk 2 - one sentence>"
  ],
  "open_questions": [
    "<question 1 the user should answer before approving>",
    "<question 2>"
  ],
  "estimated_items": <integer: rough count of changeset items a Dev-mode run would produce>
}}

RULES:
1. The `steps` list must have AT LEAST one step. Between 1 and 8 steps
   is typical. Never more than 12.
2. If the Feasibility Assessor flagged permission gaps or conflicts,
   surface them as `risks` entries - don't hide them.
3. If there are genuine ambiguities that would change the implementation,
   add them to `open_questions`. Empty list is fine if everything's clear.
4. `doctypes_touched` is the deduplicated list of Frappe doctypes the
   plan would create or modify. For a notification this might just be
   `["Notification"]`; for a workflow that adds a custom field it would
   be `["Workflow", "Custom Field"]`.
5. Do NOT output any code, JSON snippets inside strings, or field-level
   schemas. The plan is a summary, not an implementation - the Developer
   agent will fill in the details when the user approves.
6. Do NOT narrate your reasoning or mention the input sections. The
   Final Answer is ONLY the JSON object.
"""

_PLAN_DOC_EXPECTED_OUTPUT = (
	'A single JSON object with keys: title, summary, steps (array of '
	'{order, action, rationale, doctype}), doctypes_touched, risks, '
	'open_questions, estimated_items.'
)


def build_plan_crew(
	user_prompt: str,
	user_context: dict | None = None,
	site_config: dict | None = None,
	custom_tools: dict | None = None,
) -> tuple[Crew, "CrewState"]:
	"""Build the 3-agent Plan crew.

	Returns (crew, state) in the same shape as `build_alfred_crew` so the
	pipeline's run_crew helper works unchanged.

	Args:
		user_prompt: The (already-enhanced) user request.
		user_context: User identity and roles, passed through to each task.
		site_config: LLM config from Alfred Settings.
		custom_tools: Dict keyed by role (requirement/assessment/architect)
			mapping to CrewAI tool lists. In production this is
			`build_mcp_tools(mcp_client)` restricted to the three planning
			roles.
	"""
	# Local imports to avoid a circular module graph with crew.py.
	from alfred.agents.crew import CrewState, TASK_DESCRIPTIONS
	from alfred.agents.definitions import build_agents

	user_context = user_context or {}
	site_config = site_config or {}
	state = CrewState()

	agents_map = build_agents(site_config=site_config, custom_tools=custom_tools)

	format_vars = {
		"prompt": user_prompt,
		"user_context": json.dumps(user_context, indent=2),
		# These three context fields are placeholders for the Architect task;
		# CrewAI fills them from upstream task output via the `context=` kwarg,
		# not via string interpolation. We still pass them so the Requirement /
		# Assessment tasks can format their own description templates.
		"requirements": "<from Requirement Analyst>",
		"assessment": "<from Feasibility Assessor>",
		"design": "<from Solution Architect>",
		"changeset": "",  # unused in Plan mode
		"validation": "",  # unused in Plan mode
	}

	# ── Task 1: Gather requirements ────────────────────────────────
	req_tmpl = TASK_DESCRIPTIONS["gather_requirements"]
	req_task = Task(
		description=req_tmpl["description"].format(**format_vars),
		expected_output=req_tmpl["expected_output"],
		agent=agents_map["requirement"],
		human_input=False,
		callback=make_condenser_callback("gather_requirements"),
	)

	# ── Task 2: Assess feasibility ─────────────────────────────────
	assess_tmpl = TASK_DESCRIPTIONS["assess_feasibility"]
	assess_task = Task(
		description=assess_tmpl["description"].format(**format_vars),
		expected_output=assess_tmpl["expected_output"],
		agent=agents_map["assessment"],
		human_input=False,
		context=[req_task],
		callback=make_condenser_callback("assess_feasibility"),
	)

	# ── Task 3: Design + produce plan doc (the terminal task) ──────
	#
	# Single Architect task that replaces BOTH the design_solution and
	# the developer/tester/deployer tasks from the Dev pipeline. The
	# Architect agent outputs the plan doc JSON directly - no Developer
	# needed because we're not generating code, and no Tester/Deployer
	# because we're not deploying anything.
	plan_doc_task = Task(
		description=_PLAN_DOC_TASK_DESCRIPTION.format(**format_vars),
		expected_output=_PLAN_DOC_EXPECTED_OUTPUT,
		agent=agents_map["architect"],
		human_input=False,
		context=[req_task, assess_task],
		# NO condenser callback - this is the terminal task and the output
		# is the artefact we want to keep verbatim.
	)

	crew = Crew(
		agents=[
			agents_map["requirement"],
			agents_map["assessment"],
			agents_map["architect"],
		],
		tasks=[req_task, assess_task, plan_doc_task],
		process=Process.sequential,
		memory=False,
		verbose=True,
		max_rpm=site_config.get("max_tasks_per_user_per_hour", 20),
	)
	# Named distinctly so downstream post-processing can tell this apart
	# from a dev-mode changeset run.
	crew._alfred_task_names = [
		"gather_requirements",
		"assess_feasibility",
		"generate_plan_doc",
	]

	return crew, state
