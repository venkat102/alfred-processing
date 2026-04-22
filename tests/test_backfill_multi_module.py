from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


def _dt(data):
	return {"op": "create", "doctype": "DocType", "data": data}


def test_secondary_modules_contribute_roles_deduped():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	roles = {p["role"] for p in out[0]["data"]["permissions"]}
	assert "Accounts Manager" in roles
	assert "Accounts User" in roles
	assert "Projects Manager" in roles
	assert "Projects User" in roles
	assert "System Manager" in roles


def test_primary_naming_wins_over_secondary():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	assert out[0]["data"]["autoname"].startswith("format:ACC-")


def test_unknown_secondary_is_skipped():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["not_a_real_module"],
	)
	roles = {p["role"] for p in out[0]["data"]["permissions"]}
	assert "Accounts Manager" in roles
	assert "Projects Manager" not in roles


def test_no_secondary_modules_matches_v2_behaviour():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out_v2 = backfill_defaults_raw(changes, module="accounts")
	out_v3 = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=[],
	)
	assert out_v2 == out_v3


def test_secondary_permissions_rationale_mentions_secondary_context():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	perms_meta = out[0]["field_defaults_meta"]["permissions"]
	assert perms_meta["source"] == "default"
	# Rationale now references both primary and secondary contributions
	rationale = perms_meta["rationale"]
	assert "Accounts" in rationale
	assert "Projects" in rationale
	assert "secondary context" in rationale


def test_primary_user_perms_still_preserved_with_secondaries():
	# User-provided primary perms aren't overwritten when secondary is layered
	changes = [_dt({
		"name": "X", "module": "Custom",
		"permissions": [{"role": "My Custom Role", "read": 1, "write": 1, "create": 1, "delete": 0}],
	})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	roles = [p["role"] for p in out[0]["data"]["permissions"]]
	assert "My Custom Role" in roles
	# Only one instance (dedup by role)
	assert roles.count("My Custom Role") == 1
