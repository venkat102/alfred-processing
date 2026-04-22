from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


def _doctype_change(data):
	return {"op": "create", "doctype": "DocType", "data": data}


def test_module_none_behaves_like_v1():
	changes = [_doctype_change({"name": "Book", "module": "Custom"})]
	out = backfill_defaults_raw(changes)  # no module kwarg
	perms = out[0]["data"]["permissions"]
	roles = {p["role"] for p in perms}
	assert "System Manager" in roles
	assert "Accounts Manager" not in roles


def test_module_accounts_adds_accounts_roles():
	changes = [_doctype_change({"name": "Voucher", "module": "Custom"})]
	out = backfill_defaults_raw(changes, module="accounts")
	perms = out[0]["data"]["permissions"]
	roles = {p["role"] for p in perms}
	assert "System Manager" in roles
	assert "Accounts Manager" in roles
	assert "Accounts User" in roles


def test_module_accounts_swaps_defaulted_autoname():
	changes = [_doctype_change({"name": "Voucher", "module": "Custom"})]
	out = backfill_defaults_raw(changes, module="accounts")
	assert out[0]["data"]["autoname"] == "format:ACC-.YYYY.-.####"
	meta = out[0]["field_defaults_meta"]["autoname"]
	assert meta["source"] == "default"
	assert "Accounts" in meta["rationale"] or "ACC" in meta["rationale"]


def test_module_accounts_does_not_swap_user_provided_autoname():
	changes = [_doctype_change({
		"name": "Voucher", "module": "Custom", "autoname": "field:name",
	})]
	out = backfill_defaults_raw(changes, module="accounts")
	assert out[0]["data"]["autoname"] == "field:name"
	assert out[0]["field_defaults_meta"]["autoname"]["source"] == "user"


def test_module_accounts_does_not_duplicate_permission_rows():
	changes = [_doctype_change({
		"name": "Voucher", "module": "Custom",
		"permissions": [{"role": "Accounts Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
	})]
	out = backfill_defaults_raw(changes, module="accounts")
	perms = out[0]["data"]["permissions"]
	assert sum(1 for p in perms if p["role"] == "Accounts Manager") == 1


def test_module_unknown_falls_back_to_v1_behaviour():
	changes = [_doctype_change({"name": "Voucher", "module": "Custom"})]
	out = backfill_defaults_raw(changes, module="not_a_real_module")
	perms = out[0]["data"]["permissions"]
	roles = {p["role"] for p in perms}
	assert "System Manager" in roles
	assert "Accounts Manager" not in roles


def test_applying_module_defaults_twice_is_idempotent():
	# Regression guard: running backfill + module-defaults twice on the
	# same changeset must not double-append permission rows.
	changes = [_doctype_change({"name": "Voucher", "module": "Custom"})]
	once = backfill_defaults_raw(changes, module="accounts")
	twice = backfill_defaults_raw(once, module="accounts")
	once_roles = [p["role"] for p in once[0]["data"]["permissions"]]
	twice_roles = [p["role"] for p in twice[0]["data"]["permissions"]]
	# Sorted because order is stable but we don't care about it
	assert sorted(once_roles) == sorted(twice_roles)
	# No duplicates
	assert len(twice_roles) == len(set(twice_roles))
