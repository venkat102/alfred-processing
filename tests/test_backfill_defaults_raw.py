"""Raw-dict variant tests for backfill_defaults_raw.

Pipeline-facing variant: operates on the list-of-dicts shape produced by
``_extract_changes`` (keys ``op``/``doctype``/``data``) rather than typed
``ChangesetItem`` instances.
"""

from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


def _doctype_change(data):
	return {"op": "create", "doctype": "DocType", "data": data}


def test_missing_fields_backfilled_and_annotated():
	changes = [_doctype_change({"name": "Book", "module": "Custom"})]
	out = backfill_defaults_raw(changes)
	assert out[0]["data"]["autoname"] == "autoincrement"
	assert out[0]["data"]["is_submittable"] == 0
	assert out[0]["data"]["istable"] == 0
	assert out[0]["data"]["issingle"] == 0
	assert isinstance(out[0]["data"]["permissions"], list)
	meta = out[0]["field_defaults_meta"]
	assert meta["autoname"]["source"] == "default"
	assert meta["autoname"]["rationale"]
	assert meta["module"]["source"] == "user"
	assert "rationale" not in meta["module"]


def test_user_values_preserved_and_flagged_as_user_source():
	changes = [_doctype_change({
		"name": "Book", "module": "Custom",
		"autoname": "field:title", "is_submittable": 1,
	})]
	out = backfill_defaults_raw(changes)
	assert out[0]["data"]["autoname"] == "field:title"
	assert out[0]["data"]["is_submittable"] == 1
	assert out[0]["field_defaults_meta"]["autoname"]["source"] == "user"


def test_unknown_doctype_passes_through():
	# The registry has grown over time (per-intent builders added
	# entries for Custom Field, Server Script, Notification, ...) so
	# pick a DocType that definitely has no registered defaults.
	changes = [{"op": "create", "doctype": "Nonexistent Type", "data": {"fieldname": "x"}}]
	out = backfill_defaults_raw(changes)
	assert out == changes
	assert "field_defaults_meta" not in out[0]


def test_empty_input_returns_empty_list():
	assert backfill_defaults_raw([]) == []


def test_multiple_items_handled_independently():
	# Server Script now has registered defaults, so use a DocType
	# that definitely has no registry entry for the "untouched" slot.
	changes = [
		_doctype_change({"name": "Book", "module": "Custom"}),
		{"op": "create", "doctype": "Nonexistent Type", "data": {"name": "x"}},
	]
	out = backfill_defaults_raw(changes)
	assert "field_defaults_meta" in out[0]
	assert out[1]["data"] == {"name": "x"}
	assert "field_defaults_meta" not in out[1]
