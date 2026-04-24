from alfred.handlers.post_build.backfill_defaults import backfill_defaults
from alfred.models.agent_outputs import Changeset, ChangesetItem, FieldMeta


def _doctype_item(data):
	return ChangesetItem(operation="create", doctype="DocType", data=data)


def test_missing_fields_get_default_values_from_registry():
	cs = Changeset(items=[_doctype_item({"name": "Book", "module": "Custom"})])
	out = backfill_defaults(cs)
	data = out.items[0].data
	assert data["autoname"] == "autoincrement"
	assert data["is_submittable"] == 0
	assert data["istable"] == 0
	assert data["issingle"] == 0
	assert isinstance(data["permissions"], list)


def test_missing_fields_recorded_as_default_in_meta():
	cs = Changeset(items=[_doctype_item({"name": "Book", "module": "Custom"})])
	out = backfill_defaults(cs)
	meta = out.items[0].field_defaults_meta
	assert meta is not None
	assert meta["autoname"].source == "default"
	assert meta["autoname"].rationale
	assert meta["module"].source == "user"
	assert meta["module"].rationale is None


def test_user_provided_fields_preserved():
	cs = Changeset(
		items=[_doctype_item({
			"name": "Book",
			"module": "Custom",
			"autoname": "field:title",
			"is_submittable": 1,
		})]
	)
	out = backfill_defaults(cs)
	data = out.items[0].data
	assert data["autoname"] == "field:title"
	assert data["is_submittable"] == 1
	meta = out.items[0].field_defaults_meta
	assert meta["autoname"].source == "user"
	assert meta["is_submittable"].source == "user"


def test_item_with_no_matching_registry_passes_through_untouched():
	# Custom Field was unknown when this test was written, but the
	# registry now has an entry for it. Pick a DocType that has no
	# registered defaults so the passthrough branch is actually hit.
	cs = Changeset(
		items=[ChangesetItem(operation="create", doctype="Nonexistent Type", data={"fieldname": "x"})]
	)
	out = backfill_defaults(cs)
	assert out.items[0].data == {"fieldname": "x"}
	assert out.items[0].field_defaults_meta is None


def test_preexisting_field_defaults_meta_is_respected():
	item = _doctype_item({"name": "Book", "module": "Custom", "autoname": "field:title"})
	item.field_defaults_meta = {"autoname": FieldMeta(source="user")}
	cs = Changeset(items=[item])
	out = backfill_defaults(cs)
	meta = out.items[0].field_defaults_meta
	assert meta["autoname"].source == "user"
	assert meta["autoname"].rationale is None
