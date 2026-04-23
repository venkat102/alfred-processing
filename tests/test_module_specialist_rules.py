from alfred.agents.specialists.module_specialist import run_rule_validation
from alfred.models.agent_outputs import ValidationNote


def test_submittable_doctype_without_gl_hook_triggers_advisory():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Accounts Voucher", "is_submittable": 1, "module": "Accounts"},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert any(n.source == "module_rule:accounts_submittable_non_posting_doctype" for n in notes)
	submittable_note = next(
		n for n in notes if n.source == "module_rule:accounts_submittable_non_posting_doctype"
	)
	assert submittable_note.severity == "advisory"
	assert submittable_note.changeset_index == 0


def test_non_submittable_doctype_does_not_trigger_gl_warning():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Ledger Note", "is_submittable": 0, "module": "Accounts"},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert not any(n.source == "module_rule:accounts_submittable_non_posting_doctype" for n in notes)


def test_doctype_without_accounts_manager_triggers_advisory():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Accounts Voucher", "module": "Accounts"},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert any(n.source == "module_rule:accounts_needs_accounts_manager_perm" for n in notes)
	adv = next(
		n for n in notes if n.source == "module_rule:accounts_needs_accounts_manager_perm"
	)
	assert adv.severity == "advisory"


def test_non_doctype_item_ignored_by_doctype_rule():
	changes = [
		{"op": "create", "doctype": "Custom Field", "data": {"fieldname": "x"}},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert notes == []


def test_unknown_module_returns_empty():
	changes = [{"op": "create", "doctype": "DocType", "data": {}}]
	notes = run_rule_validation(module="not_a_real_module", changes=changes)
	assert notes == []


def test_empty_changes_returns_empty():
	notes = run_rule_validation(module="accounts", changes=[])
	assert notes == []


def test_rule_notes_are_validation_note_instances():
	changes = [{"op": "create", "doctype": "DocType", "data": {"is_submittable": 1}}]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert all(isinstance(n, ValidationNote) for n in notes)


def test_cap_secondary_severity_blocker_becomes_warning():
	from alfred.agents.specialists.module_specialist import cap_secondary_severity
	notes = [
		ValidationNote(severity="blocker", source="module_rule:x", issue="a"),
		ValidationNote(severity="warning", source="module_rule:y", issue="b"),
		ValidationNote(severity="advisory", source="module_rule:z", issue="c"),
	]
	capped = cap_secondary_severity(notes)
	assert capped[0].severity == "warning"
	assert capped[1].severity == "warning"
	assert capped[2].severity == "advisory"
	# Original list unmodified
	assert notes[0].severity == "blocker"


def test_cap_secondary_severity_preserves_other_fields():
	from alfred.agents.specialists.module_specialist import cap_secondary_severity
	notes = [
		ValidationNote(
			severity="blocker", source="module_rule:x", issue="a",
			field="f", fix="do f", changeset_index=3,
		),
	]
	capped = cap_secondary_severity(notes)
	assert capped[0].source == "module_rule:x"
	assert capped[0].issue == "a"
	assert capped[0].field == "f"
	assert capped[0].fix == "do f"
	assert capped[0].changeset_index == 3
