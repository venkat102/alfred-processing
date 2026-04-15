"""Pydantic model for Plan mode output.

Phase C of the three-mode chat feature. Plan mode runs a 3-agent crew
(Requirement, Assessment, Architect) that produces a structured plan
document instead of a deployable changeset. The plan doc is shown to the
user, who can refine it or approve it to trigger a Dev-mode run with the
plan as the spec.

Deliberately kept small and flat - this is what the user reads, not a
full requirement/architecture spec. The full RequirementSpec /
ArchitectureBlueprint models in `agent_outputs.py` are still what the
agents think in, but the final `generate_plan_doc` task condenses those
into this user-facing shape.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class PlanStep(BaseModel):
	"""One numbered step in the proposed plan.

	A step is one concrete action the user will see if they approve the
	plan - typically creating or modifying one Frappe document (a
	Notification, Server Script, Custom Field, ...). Keep rationale to
	one sentence so the panel UI stays scannable.
	"""

	order: int = Field(..., description="1-based step number")
	action: str = Field(..., description="What this step does, one line")
	rationale: str = Field("", description="Why this step is needed, one sentence")
	doctype: str | None = Field(
		None, description="Primary Frappe doctype this step touches, if any"
	)


class PlanDoc(BaseModel):
	"""A user-facing plan document produced by Plan mode.

	Shape is chosen to be both LLM-friendly (the Architect task outputs
	this JSON) and UI-friendly (`PlanDocPanel.vue` renders each field as
	a distinct section with approve / refine buttons).
	"""

	title: str = Field(..., description="Short title for the plan")
	summary: str = Field(..., description="One-paragraph summary of what will be built")
	steps: list[PlanStep] = Field(
		default_factory=list, description="Numbered list of plan steps"
	)
	doctypes_touched: list[str] = Field(
		default_factory=list,
		description="Frappe doctypes the plan creates or modifies",
	)
	risks: list[str] = Field(
		default_factory=list,
		description="Known risks the user should be aware of before approving",
	)
	open_questions: list[str] = Field(
		default_factory=list,
		description="Questions the user should answer before the plan is executed",
	)
	estimated_items: int = Field(
		0, description="Rough count of changeset items a Dev-mode run will produce"
	)

	@classmethod
	def stub(cls, title: str = "Plan", summary: str = "") -> "PlanDoc":
		"""Build an empty fallback plan doc for error paths."""
		return cls(title=title, summary=summary or "No plan could be generated.")
