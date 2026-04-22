import pytest
from pydantic import ValidationError

from alfred.models.agent_outputs import ValidationNote


def test_minimal_fields_required():
	note = ValidationNote(severity="warning", source="module_rule:x", issue="y")
	assert note.severity == "warning"
	assert note.source == "module_rule:x"
	assert note.issue == "y"
	assert note.field is None
	assert note.fix is None
	assert note.changeset_index is None


def test_severity_must_be_known():
	with pytest.raises(ValidationError):
		ValidationNote(severity="bogus", source="x", issue="y")


def test_all_fields_accepted():
	note = ValidationNote(
		severity="blocker",
		source="module_specialist:accounts",
		field="permissions",
		issue="missing role",
		fix="add Accounts Manager",
		changeset_index=0,
	)
	assert note.fix == "add Accounts Manager"
	assert note.changeset_index == 0


def test_serialization_round_trip():
	note = ValidationNote(severity="advisory", source="s", issue="i")
	dumped = note.model_dump()
	restored = ValidationNote.model_validate(dumped)
	assert restored.severity == "advisory"
