import pytest

from alfred.registry.module_loader import ModuleRegistry, UnknownModuleError


@pytest.fixture(autouse=True)
def _reset():
	ModuleRegistry._instance = None
	yield
	ModuleRegistry._instance = None


def test_load_returns_registry_with_known_modules():
	registry = ModuleRegistry.load()
	assert "accounts" in registry.modules()


def test_get_returns_kb_dict():
	registry = ModuleRegistry.load()
	kb = registry.get("accounts")
	assert kb["module"] == "accounts"
	assert kb["display_name"] == "Accounts"
	assert kb["frappe_module_key"] == "Accounts"
	assert "conventions" in kb
	assert "validation_rules" in kb


def test_get_unknown_module_raises():
	registry = ModuleRegistry.load()
	with pytest.raises(UnknownModuleError):
		registry.get("not_a_real_module")


def test_load_returns_singleton():
	first = ModuleRegistry.load()
	second = ModuleRegistry.load()
	assert first is second


def test_for_doctype_matches_detection_hints():
	registry = ModuleRegistry.load()
	kb = registry.for_doctype("Sales Invoice")
	assert kb is not None
	assert kb["module"] == "accounts"


def test_for_doctype_unknown_returns_none():
	registry = ModuleRegistry.load()
	assert registry.for_doctype("Nonexistent Custom DocType Xyz") is None


def test_detect_prefers_target_doctype_over_keywords():
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(
		prompt="random prompt with no keyword hits",
		target_doctype="Sales Invoice",
	)
	assert module_key == "accounts"
	assert confidence == "high"


def test_detect_falls_back_to_keyword_hints():
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(
		prompt="I want to set up a journal entry for adjustment",
		target_doctype=None,
	)
	assert module_key == "accounts"
	assert confidence == "medium"


def test_detect_returns_none_when_no_match():
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(
		prompt="hello goodbye",
		target_doctype=None,
	)
	assert module_key is None
	assert confidence is None


def test_custom_module_registered():
	registry = ModuleRegistry.load()
	assert "custom" in registry.modules()
	kb = registry.get("custom")
	assert kb["display_name"] == "Custom"


def test_custom_detected_via_keyword_hint():
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(
		prompt="I need a simple doctype for tracking weekly meetings",
		target_doctype=None,
	)
	assert module_key == "custom"
	assert confidence == "medium"


def test_accounts_beats_custom_when_both_could_match():
	# Prompt contains both "custom" phrasing and an Accounts keyword hint.
	# Accounts loads first alphabetically, so its "invoice" hint wins.
	registry = ModuleRegistry.load()
	module_key, _confidence = registry.detect(
		prompt="build a custom invoice tracker",
		target_doctype=None,
	)
	assert module_key == "accounts"


def test_word_boundary_prevents_substring_false_positive():
	# "accountant" should NOT match accounts's "accounting" keyword.
	registry = ModuleRegistry.load()
	module_key, _c = registry.detect(
		prompt="I need a table to track accountants by certification",
		target_doctype=None,
	)
	# Either no match (None) or another module - crucially, not accounts on
	# the "accounting" fragment. Result depends on other module keywords;
	# what matters is "accounting" didn't hit.
	if module_key == "accounts":
		pytest.fail("substring false positive: 'accountants' matched 'accounting'")


@pytest.mark.parametrize("doctype,expected_module", [
	("Employee", "hr"),
	("Leave Application", "hr"),
	("Attendance", "hr"),
	("Item", "stock"),
	("Warehouse", "stock"),
	("Stock Entry", "stock"),
	("Customer", "selling"),
	("Sales Order", "selling"),
	("Quotation", "selling"),
	("Supplier", "buying"),
	("Purchase Order", "buying"),
	("Request for Quotation", "buying"),
	("Custom DocType Xyz", None),
])
def test_for_doctype_across_modules(doctype, expected_module):
	registry = ModuleRegistry.load()
	kb = registry.for_doctype(doctype)
	if expected_module is None:
		assert kb is None
	else:
		assert kb is not None
		assert kb["module"] == expected_module


@pytest.mark.parametrize("phrase,expected_module", [
	("show me leave applications by department", "hr"),
	("track attendance for a shift type", "hr"),
	("add a warehouse-specific validation", "stock"),
	("build a stock entry preview dashboard", "stock"),
	("customize the sales order form", "selling"),
	("add a sales taxes rule", "selling"),
	("create a supplier scorecard doctype", "buying"),
	("make a purchase order review helper", "buying"),
])
def test_keyword_detection_across_modules(phrase, expected_module):
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(prompt=phrase, target_doctype=None)
	assert module_key == expected_module
	assert confidence == "medium"
