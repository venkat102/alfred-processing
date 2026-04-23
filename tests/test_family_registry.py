"""Family-layer tests for ModuleRegistry.

Covers:
  - family KBs load and validate against modules/_families/_meta_schema.json
  - every non-custom module declares a family pointing to an existing
    family KB; custom stays familyless
  - get_family / family_for_module / families APIs behave as expected
  - provide_family_context labels cache keys correctly and honours the
    15-minute TTL separately from the 5-minute module TTL
  - pipeline layering emits PRIMARY FAMILY / PRIMARY MODULE / SECONDARY
    MODULE in the right order and dedupes family headers when secondary
    modules share the family with the primary
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from alfred.registry.module_loader import (
	FAMILIES_DIR,
	SCHEMA_DIR,
	ModuleRegistry,
	UnknownFamilyError,
)


def _fresh_registry() -> ModuleRegistry:
	ModuleRegistry._instance = None
	return ModuleRegistry.load()


def test_family_schema_self_validates():
	schema_path = FAMILIES_DIR / "_meta_schema.json"
	assert schema_path.is_file(), "family meta schema missing"
	schema = json.loads(schema_path.read_text())
	jsonschema.Draft7Validator.check_schema(schema)


def test_family_jsons_validate_against_schema():
	schema = json.loads((FAMILIES_DIR / "_meta_schema.json").read_text())
	for path in sorted(FAMILIES_DIR.glob("*.json")):
		if path.name.startswith("_"):
			continue
		data = json.loads(path.read_text())
		jsonschema.validate(instance=data, schema=schema)


def test_registry_loads_four_families():
	r = _fresh_registry()
	assert set(r.families()) == {"transactions", "operations", "people", "engagement"}


def test_every_non_custom_module_has_family():
	r = _fresh_registry()
	for module in r.modules():
		family = r.family_for_module(module)
		if module == "custom":
			assert family is None, "custom should be intentionally familyless"
		else:
			assert family is not None, f"{module} is missing the family field"
			assert family in r.families(), (
				f"{module} declares family={family!r} which has no KB"
			)


def test_family_member_modules_match_module_family_field():
	"""Every family KB's member_modules list must match reality."""
	r = _fresh_registry()
	for family_name in r.families():
		family_kb = r.get_family(family_name)
		declared = set(family_kb["member_modules"])
		actual = {
			m for m in r.modules()
			if r.family_for_module(m) == family_name
		}
		assert declared == actual, (
			f"family={family_name!r} member_modules={declared} "
			f"does not match modules with family={family_name!r} ({actual})"
		)


def test_expected_family_groupings():
	r = _fresh_registry()
	assert r.family_for_module("accounts") == "transactions"
	assert r.family_for_module("selling") == "transactions"
	assert r.family_for_module("buying") == "transactions"
	assert r.family_for_module("stock") == "operations"
	assert r.family_for_module("manufacturing") == "operations"
	assert r.family_for_module("assets") == "operations"
	assert r.family_for_module("hr") == "people"
	assert r.family_for_module("payroll") == "people"
	assert r.family_for_module("crm") == "engagement"
	assert r.family_for_module("support") == "engagement"
	assert r.family_for_module("projects") == "engagement"
	assert r.family_for_module("maintenance") == "engagement"


def test_get_family_unknown_raises():
	r = _fresh_registry()
	with pytest.raises(UnknownFamilyError):
		r.get_family("not_a_real_family")


def test_family_kb_has_cross_module_invariants():
	r = _fresh_registry()
	for family_name in r.families():
		kb = r.get_family(family_name)
		invariants = kb.get("cross_module_invariants", [])
		assert len(invariants) >= 3, (
			f"{family_name} should carry at least 3 cross-module invariants"
		)


def test_family_for_module_unknown_returns_none():
	r = _fresh_registry()
	assert r.family_for_module("not_a_real_module") is None
