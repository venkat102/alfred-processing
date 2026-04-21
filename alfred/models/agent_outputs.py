"""Pydantic models for agent output schemas.

Each agent produces structured output validated against these models.
Models serve as documentation, validation, and contract between agents.
"""

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ── Shared Enums ─────────────────────────────────────────────────

class CustomizationType(str, Enum):
	DOCTYPE = "DocType"
	CUSTOM_FIELD = "Custom Field"
	SERVER_SCRIPT = "Server Script"
	CLIENT_SCRIPT = "Client Script"
	WORKFLOW = "Workflow"
	REPORT = "Report"
	NOTIFICATION = "Notification"
	PRINT_FORMAT = "Print Format"
	PROPERTY_SETTER = "Property Setter"


class Verdict(str, Enum):
	AI_CAN_HANDLE = "ai_can_handle"
	NEEDS_HUMAN = "needs_human"
	PARTIAL = "partial"


class Complexity(str, Enum):
	LOW = "low"
	MEDIUM = "medium"
	HIGH = "high"


class RiskLevel(str, Enum):
	LOW = "low"
	MEDIUM = "medium"
	HIGH = "high"


class ChangeOperation(str, Enum):
	CREATE = "create"
	UPDATE = "update"
	DELETE = "delete"


class ValidationStatus(str, Enum):
	PASS = "PASS"
	FAIL = "FAIL"


class IssueSeverity(str, Enum):
	CRITICAL = "critical"
	WARNING = "warning"


class DeploymentApproval(str, Enum):
	APPROVED = "approved"
	REJECTED = "rejected"
	PENDING = "pending"


class DeployStepStatus(str, Enum):
	SUCCESS = "success"
	FAILED = "failed"
	SKIPPED = "skipped"
	ROLLED_BACK = "rolled_back"


# ── Task 3.1: Requirement Agent Output ───────────────────────────

class CustomizationItem(BaseModel):
	"""A single customization identified in the requirements."""
	type: CustomizationType
	name: str = Field(..., description="Proposed name for this customization")
	description: str = Field(..., description="What this does and why it's needed")
	fields: list[dict[str, Any]] = Field(default_factory=list, description="Field definitions for DocTypes")
	needs_workflow: bool = False
	needs_server_script: bool = False
	needs_client_script: bool = False


class RequirementSpec(BaseModel):
	"""Structured output from the Requirement Agent."""
	summary: str = Field(..., description="Brief description of what's being built")
	customizations_needed: list[CustomizationItem] = Field(default_factory=list)
	dependencies: list[str] = Field(default_factory=list, description="Existing DocTypes/features this depends on")
	open_questions: list[str] = Field(default_factory=list, description="Remaining ambiguities")


# ── Task 3.2: Assessment Agent Output ────────────────────────────

class PermissionCheckItem(BaseModel):
	"""Result of checking a single permission."""
	customization_type: str
	required_role: str
	permitted: bool
	reason: str = ""


class PermissionCheckResult(BaseModel):
	"""Deterministic permission check result."""
	passed: bool
	failed: list[PermissionCheckItem] = Field(default_factory=list)


class AssessmentResult(BaseModel):
	"""Structured output from the Assessment Agent."""
	verdict: Verdict
	permission_check: PermissionCheckResult
	complexity: Complexity
	risk_factors: list[str] = Field(default_factory=list)
	conflicts: list[str] = Field(default_factory=list)
	estimated_changes: int = 0
	human_escalation_reason: str | None = None


# ── Task 3.3: Architect Agent Output ─────────────────────────────

class FieldDesign(BaseModel):
	"""Design specification for a single field."""
	fieldname: str
	fieldtype: str
	label: str
	options: str = ""
	reqd: int = 0
	default: str = ""
	in_list_view: int = 0
	description: str = ""


class PermissionDesign(BaseModel):
	"""Permission rule design for a DocType."""
	role: str
	read: int = 1
	write: int = 0
	create: int = 0
	delete: int = 0
	submit: int = 0


class DocumentDesign(BaseModel):
	"""Design specification for a single document operation."""
	order: int
	operation: ChangeOperation
	doctype: str = Field(..., description="Frappe document type (DocType, Server Script, etc.)")
	name: str = Field(..., description="Document name")
	design: dict[str, Any] = Field(default_factory=dict, description="Full design specification")


class ArchitectureBlueprint(BaseModel):
	"""Structured output from the Architect Agent."""
	documents: list[DocumentDesign] = Field(default_factory=list)
	deployment_order: list[str] = Field(default_factory=list)
	rollback_safe: bool = True


# ── Task 3.4: Developer Agent Output ─────────────────────────────

class FieldMeta(BaseModel):
	"""Provenance annotation for a single key inside ``ChangesetItem.data``.

	Written by the per-intent Builder specialist and by the defaults
	backfill post-processor. Consumed by ``alfred_client`` to render
	default rows with a "default" pill and rationale tooltip. Server-side
	Frappe deploy ignores this field.
	"""

	source: Literal["user", "default"]
	rationale: Optional[str] = None


class ChangesetItem(BaseModel):
	"""A single document operation in the changeset."""
	operation: ChangeOperation
	doctype: str = Field(..., description="Frappe document type")
	data: dict[str, Any] = Field(..., description="Complete document definition")
	field_defaults_meta: Optional[dict[str, FieldMeta]] = Field(
		None,
		description=(
			"Per-key provenance for values inside ``data``: which came from "
			"the user, which were filled from the intent registry default. "
			"Client-only; Frappe deploy ignores."
		),
	)


class Changeset(BaseModel):
	"""Structured output from the Developer Agent."""
	items: list[ChangesetItem] = Field(default_factory=list)


# ── Task 3.5: Tester Agent Output ────────────────────────────────

class ValidationIssue(BaseModel):
	"""A single validation issue found by the Tester."""
	severity: IssueSeverity
	item: str = Field(..., description="Which changeset item has the issue")
	issue: str = Field(..., description="What the issue is")
	fix: str = Field("", description="How to fix it")
	line: int | None = None


class TestReport(BaseModel):
	"""Structured output from the Tester Agent."""
	# Tell pytest not to collect this as a test class (starts with "Test")
	__test__ = False

	status: ValidationStatus
	issues: list[ValidationIssue] = Field(default_factory=list)
	summary: str = ""
	static_checks_passed: bool = True
	dry_run_checks_passed: bool = True
	permission_checks_passed: bool = True


# ── Task 3.6: Deployer Agent Output ──────────────────────────────

class DeployStep(BaseModel):
	"""A single deployment step result."""
	order: int
	operation: ChangeOperation
	doctype: str
	name: str
	status: DeployStepStatus


class DeploymentResult(BaseModel):
	"""Structured output from the Deployer Agent."""
	plan: list[DeployStep] = Field(default_factory=list)
	approval: DeploymentApproval = DeploymentApproval.PENDING
	execution_log: list[DeployStep] = Field(default_factory=list)
	rollback_data: list[dict[str, Any]] = Field(default_factory=list)
	documents_created: list[str] = Field(default_factory=list)
	documents_modified: list[str] = Field(default_factory=list)
	error: str | None = None
