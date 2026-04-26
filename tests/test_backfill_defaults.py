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
	# Use a doctype that genuinely has no intent registry entry.
	# (Custom Field did when this test was written, but a create_custom_field
	# registry has since landed - the "no matching registry" assertion has
	# to be anchored to a truly unregistered doctype to stay meaningful.)
	cs = Changeset(
		items=[ChangesetItem(operation="create", doctype="Sales Invoice", data={"customer": "ACME"})]
	)
	out = backfill_defaults(cs)
	assert out.items[0].data == {"customer": "ACME"}
	assert out.items[0].field_defaults_meta is None


def test_intent_gating_skips_items_whose_doctype_is_not_intent_target():
	# create_doctype intent's target is DocType. A Custom Field item
	# slipped into the same changeset should NOT receive create_custom_field
	# defaults - intent classification pins backfill to ONE target doctype.
	cs = Changeset(items=[
		ChangesetItem(operation="create", doctype="Custom Field", data={"fieldname": "x"}),
	])
	out = backfill_defaults(cs, intent="create_doctype")
	assert out.items[0].data == {"fieldname": "x"}
	assert out.items[0].field_defaults_meta is None


def test_intent_gating_applies_to_matching_doctype():
	# Same intent, but the item's doctype DOES match - backfill runs.
	cs = Changeset(items=[
		ChangesetItem(operation="create", doctype="DocType", data={"name": "Book", "module": "Custom"}),
	])
	out = backfill_defaults(cs, intent="create_doctype")
	assert out.items[0].data.get("autoname") == "autoincrement"
	assert out.items[0].field_defaults_meta["autoname"].source == "default"


def test_intent_gating_unknown_intent_passes_through():
	cs = Changeset(items=[
		ChangesetItem(operation="create", doctype="DocType", data={"name": "Book"}),
	])
	out = backfill_defaults(cs, intent="not_a_real_intent")
	assert out.items[0].data == {"name": "Book"}
	assert out.items[0].field_defaults_meta is None


def test_preexisting_field_defaults_meta_is_respected():
	item = _doctype_item({"name": "Book", "module": "Custom", "autoname": "field:title"})
	item.field_defaults_meta = {"autoname": FieldMeta(source="user")}
	cs = Changeset(items=[item])
	out = backfill_defaults(cs)
	meta = out.items[0].field_defaults_meta
	assert meta["autoname"].source == "user"
	assert meta["autoname"].rationale is None
