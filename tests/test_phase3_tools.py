"""Tests for Phase 3 agent tools: permission checks and code validation."""



from alfred.models.agent_outputs import (
	ArchitectureBlueprint,
	AssessmentResult,
	Changeset,
	DeploymentResult,
	RequirementSpec,
	TestReport,
)
from alfred.tools.code_validation import (
	validate_changeset_order,
	validate_doctype_definition,
	validate_js_syntax,
	validate_python_syntax,
	validate_workflow_definition,
)
from alfred.tools.permission_checks import (
	assess_complexity,
	check_escalation_needed,
	check_permissions,
)

# ── Permission Checks (Task 3.2) ─────────────────────────────────


class TestPermissionMatrix:
	"""Deterministic permission checks - same input always same output."""

	def test_system_manager_passes_all(self):
		spec = {
			"customizations_needed": [
				{"type": "DocType"}, {"type": "Custom Field"},
				{"type": "Server Script"}, {"type": "Workflow"},
			]
		}
		result = check_permissions(spec, ["System Manager"])
		assert result["passed"] is True
		assert result["failed"] == []

	def test_hr_manager_blocked(self):
		spec = {"customizations_needed": [{"type": "DocType"}, {"type": "Server Script"}]}
		result = check_permissions(spec, ["HR Manager", "Employee"])
		assert result["passed"] is False
		assert len(result["failed"]) == 2

	def test_workflow_manager_partial(self):
		spec = {
			"customizations_needed": [
				{"type": "Workflow"},
				{"type": "Server Script"},
			]
		}
		result = check_permissions(spec, ["Workflow Manager"])
		assert result["passed"] is False
		assert len(result["failed"]) == 1
		assert result["failed"][0]["customization_type"] == "Server Script"

	def test_unknown_type_blocked(self):
		spec = {"customizations_needed": [{"type": "Custom App"}]}
		result = check_permissions(spec, ["System Manager"])
		assert result["passed"] is False
		assert "Unrecognized" in result["failed"][0]["reason"]

	def test_empty_spec_passes(self):
		result = check_permissions({"customizations_needed": []}, ["Employee"])
		assert result["passed"] is True

	def test_complexity_low(self):
		assert assess_complexity({"customizations_needed": [{"type": "DocType"}]}) == "low"

	def test_complexity_medium(self):
		assert assess_complexity({"customizations_needed": [{}] * 4}) == "medium"

	def test_complexity_high(self):
		assert assess_complexity({"customizations_needed": [{}] * 7}) == "high"

	def test_escalation_too_many_changes(self):
		spec = {"customizations_needed": [{}] * 12}
		reason = check_escalation_needed(spec)
		assert reason is not None
		assert "12 changes" in reason

	def test_escalation_app_level(self):
		spec = {"customizations_needed": [{"description": "modify hooks.py"}]}
		reason = check_escalation_needed(spec)
		assert reason is not None
		assert "hooks.py" in reason

	def test_no_escalation_normal(self):
		spec = {"customizations_needed": [{"type": "DocType"}]}
		assert check_escalation_needed(spec) is None

	def test_deterministic(self):
		"""Same input always produces same output."""
		spec = {"customizations_needed": [{"type": "DocType"}, {"type": "Workflow"}]}
		roles = ["HR Manager"]
		r1 = check_permissions(spec, roles)
		r2 = check_permissions(spec, roles)
		assert r1 == r2


# ── Python Validation (Task 3.5) ─────────────────────────────────


class TestPythonValidation:
	def test_valid_code(self):
		code = """
frappe.has_permission("Leave Request", "write")
doc = frappe.get_doc("Leave Request", name)
doc.status = "Approved"
doc.save()
"""
		result = validate_python_syntax(code)
		assert result["valid"] is True

	def test_syntax_error(self):
		code = "def validate(doc, method:"
		result = validate_python_syntax(code)
		assert result["valid"] is False
		assert any(e["type"] == "syntax_error" for e in result["errors"])

	def test_forbidden_import_os(self):
		code = "import os\nos.system('rm -rf /')"
		result = validate_python_syntax(code)
		assert result["valid"] is False
		assert any(e["type"] == "forbidden_import" for e in result["errors"])

	def test_forbidden_import_subprocess(self):
		code = "from subprocess import call\ncall(['ls'])"
		result = validate_python_syntax(code)
		assert result["valid"] is False
		assert any("subprocess" in e["message"] for e in result["errors"])

	def test_forbidden_eval(self):
		code = 'result = eval("1+1")'
		result = validate_python_syntax(code)
		assert result["valid"] is False
		assert any(e["type"] == "forbidden_function" for e in result["errors"])

	def test_forbidden_raw_sql(self):
		code = 'frappe.db.sql("SELECT * FROM tabItem WHERE name=%s", name)'
		result = validate_python_syntax(code)
		assert result["valid"] is False
		assert any("raw SQL" in e["message"] for e in result["errors"])

	def test_missing_permission_check(self):
		code = 'docs = frappe.get_all("Item", fields=["name"])'
		result = validate_python_syntax(code)
		assert any(e["type"] == "missing_permission_check" for e in result["errors"])

	def test_with_permission_check_passes(self):
		code = """
frappe.has_permission("Item", "read")
docs = frappe.get_all("Item", fields=["name"])
"""
		result = validate_python_syntax(code)
		assert not any(e["type"] == "missing_permission_check" for e in result["errors"])

	def test_hardcoded_email(self):
		code = 'frappe.sendmail(recipients=["admin@evil.com"], subject="test")'
		result = validate_python_syntax(code)
		assert any(e["type"] == "hardcoded_email" for e in result["errors"])


# ── JavaScript Validation (Task 3.5) ─────────────────────────────


class TestJSValidation:
	def test_valid_js(self):
		code = """
frappe.ui.form.on("Leave Request", {
	refresh: function(frm) {
		frm.set_value("status", "Draft");
	}
});
"""
		result = validate_js_syntax(code)
		assert result["valid"] is True

	def test_unmatched_bracket(self):
		code = "function test() { return 1;"
		result = validate_js_syntax(code)
		assert result["valid"] is False
		assert any("Unclosed" in e["message"] for e in result["errors"])


# ── DocType Validation (Task 3.5) ────────────────────────────────


class TestDocTypeValidation:
	def test_valid_doctype(self):
		dt = {
			"name": "Book",
			"module": "Alfred",
			"fields": [
				{"fieldname": "title", "fieldtype": "Data", "label": "Title"},
				{"fieldname": "author", "fieldtype": "Data", "label": "Author"},
			],
			"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
		}
		result = validate_doctype_definition(dt)
		assert result["valid"] is True

	def test_wrong_module(self):
		dt = {"name": "Book", "module": "Core", "fields": [], "permissions": [{"role": "System Manager"}]}
		result = validate_doctype_definition(dt)
		assert not result["valid"]
		assert any(e["type"] == "wrong_module" for e in result["errors"])

	def test_reserved_fieldname(self):
		dt = {
			"name": "Book", "module": "Alfred",
			"fields": [{"fieldname": "name", "fieldtype": "Data", "label": "Name"}],
			"permissions": [{"role": "System Manager"}],
		}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "reserved_fieldname" for e in result["errors"])

	def test_invalid_fieldtype(self):
		dt = {
			"name": "Book", "module": "Alfred",
			"fields": [{"fieldname": "title", "fieldtype": "Dropdown", "label": "Title"}],
			"permissions": [{"role": "System Manager"}],
		}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "invalid_fieldtype" for e in result["errors"])

	def test_duplicate_fieldname(self):
		dt = {
			"name": "Book", "module": "Alfred",
			"fields": [
				{"fieldname": "title", "fieldtype": "Data", "label": "Title"},
				{"fieldname": "title", "fieldtype": "Data", "label": "Title Again"},
			],
			"permissions": [{"role": "System Manager"}],
		}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "duplicate_fieldname" for e in result["errors"])

	def test_no_permissions(self):
		dt = {"name": "Book", "module": "Alfred", "fields": [], "permissions": []}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "no_permissions" for e in result["errors"])

	def test_dangerous_admin_permission(self):
		dt = {
			"name": "Book", "module": "Alfred", "fields": [],
			"permissions": [{"role": "Administrator", "read": 1}],
		}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "dangerous_permission" for e in result["errors"])

	def test_link_without_options(self):
		dt = {
			"name": "Book", "module": "Alfred",
			"fields": [{"fieldname": "author_link", "fieldtype": "Link", "label": "Author"}],
			"permissions": [{"role": "System Manager"}],
		}
		result = validate_doctype_definition(dt)
		assert any(e["type"] == "missing_link_target" for e in result["errors"])


# ── Workflow Validation (Task 3.5) ────────────────────────────────


class TestWorkflowValidation:
	def test_valid_workflow(self):
		wf = {
			"name": "Leave Approval",
			"states": [
				{"state": "Draft", "doc_status": "0"},
				{"state": "Pending Approval", "doc_status": "0"},
				{"state": "Approved", "doc_status": "1"},
			],
			"transitions": [
				{"state": "Draft", "next_state": "Pending Approval", "action": "Submit", "allowed": "Employee"},
				{"state": "Pending Approval", "next_state": "Approved", "action": "Approve", "allowed": "HR Manager"},
			],
		}
		result = validate_workflow_definition(wf)
		assert result["valid"] is True

	def test_invalid_transition_target(self):
		wf = {
			"name": "Bad WF",
			"states": [{"state": "Draft", "doc_status": "0"}],
			"transitions": [{"state": "Draft", "next_state": "NonExistent", "action": "Go"}],
		}
		result = validate_workflow_definition(wf)
		assert any(e["type"] == "invalid_transition_target" for e in result["errors"])


# ── Changeset Order Validation (Task 3.5) ─────────────────────────


class TestChangesetOrder:
	def test_correct_order(self):
		changeset = [
			{"doctype": "DocType", "operation": "create", "data": {"name": "Author", "fields": []}},
			{"doctype": "DocType", "operation": "create", "data": {
				"name": "Book",
				"fields": [{"fieldname": "author", "fieldtype": "Link", "options": "Author"}],
			}},
		]
		result = validate_changeset_order(changeset)
		assert result["valid"] is True

	def test_wrong_order(self):
		changeset = [
			{"doctype": "DocType", "operation": "create", "data": {
				"name": "Book",
				"fields": [{"fieldname": "author", "fieldtype": "Link", "options": "Author"}],
			}},
			{"doctype": "DocType", "operation": "create", "data": {"name": "Author", "fields": []}},
		]
		result = validate_changeset_order(changeset)
		assert not result["valid"]
		assert any(e["type"] == "dependency_order" for e in result["errors"])

	def test_circular_dependency(self):
		changeset = [
			{"doctype": "DocType", "operation": "create", "data": {
				"name": "A", "fields": [{"fieldname": "b_link", "fieldtype": "Link", "options": "B"}],
			}},
			{"doctype": "DocType", "operation": "create", "data": {
				"name": "B", "fields": [{"fieldname": "a_link", "fieldtype": "Link", "options": "A"}],
			}},
		]
		result = validate_changeset_order(changeset)
		assert any(e["type"] == "circular_dependency" for e in result["errors"])


# ── Pydantic Model Validation ─────────────────────────────────────


class TestPydanticModels:
	def test_requirement_spec(self):
		spec = RequirementSpec(
			summary="Create a Book DocType",
			customizations_needed=[],
			dependencies=["User"],
		)
		assert spec.summary == "Create a Book DocType"

	def test_assessment_result(self):
		from alfred.models.agent_outputs import Complexity, PermissionCheckResult, Verdict
		result = AssessmentResult(
			verdict=Verdict.AI_CAN_HANDLE,
			permission_check=PermissionCheckResult(passed=True),
			complexity=Complexity.LOW,
		)
		assert result.verdict == "ai_can_handle"

	def test_architecture_blueprint(self):
		bp = ArchitectureBlueprint(
			documents=[],
			deployment_order=["Book"],
			rollback_safe=True,
		)
		assert bp.rollback_safe is True

	def test_changeset(self):
		from alfred.models.agent_outputs import ChangeOperation, ChangesetItem
		cs = Changeset(items=[
			ChangesetItem(operation=ChangeOperation.CREATE, doctype="DocType", data={"name": "Book"}),
		])
		assert len(cs.items) == 1

	def test_test_report(self):
		from alfred.models.agent_outputs import ValidationStatus
		report = TestReport(
			status=ValidationStatus.PASS,
			summary="All checks passed",
		)
		assert report.status == "PASS"

	def test_deployment_result(self):
		result = DeploymentResult()
		assert result.approval == "pending"
