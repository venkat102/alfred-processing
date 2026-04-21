import pytest
from pydantic import ValidationError

from alfred.models.agent_outputs import ChangesetItem, FieldMeta


def test_field_defaults_meta_defaults_to_none():
	item = ChangesetItem(operation="create", doctype="DocType", data={"name": "Book"})
	assert item.field_defaults_meta is None


def test_field_defaults_meta_accepts_dict():
	item = ChangesetItem(
		operation="create",
		doctype="DocType",
		data={"autoname": "autoincrement"},
		field_defaults_meta={
			"autoname": FieldMeta(source="default", rationale="Safe default.")
		},
	)
	assert item.field_defaults_meta is not None
	assert item.field_defaults_meta["autoname"].source == "default"
	assert item.field_defaults_meta["autoname"].rationale == "Safe default."


def test_field_meta_source_user_allows_null_rationale():
	meta = FieldMeta(source="user")
	assert meta.source == "user"
	assert meta.rationale is None


def test_field_meta_source_must_be_user_or_default():
	with pytest.raises(ValidationError):
		FieldMeta(source="invalid")


def test_serialization_round_trip():
	item = ChangesetItem(
		operation="create",
		doctype="DocType",
		data={"name": "Book"},
		field_defaults_meta={"autoname": FieldMeta(source="default", rationale="r")},
	)
	dumped = item.model_dump()
	restored = ChangesetItem.model_validate(dumped)
	assert restored.field_defaults_meta["autoname"].source == "default"
