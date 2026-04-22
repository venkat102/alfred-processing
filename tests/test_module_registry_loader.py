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
	("BOM", "manufacturing"),
	("Work Order", "manufacturing"),
	("Job Card", "manufacturing"),
	("Routing", "manufacturing"),
	("Project", "projects"),
	("Task", "projects"),
	("Timesheet", "projects"),
	("Activity Type", "projects"),
	("Asset", "assets"),
	("Asset Category", "assets"),
	("Asset Movement", "assets"),
	("Depreciation Schedule", "assets"),
	("Lead", "crm"),
	("Opportunity", "crm"),
	("Contract", "crm"),
	("Appointment", "crm"),
	("Salary Slip", "payroll"),
	("Salary Structure", "payroll"),
	("Payroll Entry", "payroll"),
	("Income Tax Slab", "payroll"),
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


def test_detect_all_returns_primary_only_when_no_secondary_keyword_match():
	registry = ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt="Customize Sales Invoice",
		target_doctype="Sales Invoice",
	)
	assert primary == "accounts"
	assert confidence == "high"
	assert secondaries == []


def test_detect_all_finds_secondary_from_keyword_when_primary_from_target():
	registry = ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt="Create a Sales Invoice that auto-creates a project task",
		target_doctype="Sales Invoice",
	)
	assert primary == "accounts"
	assert confidence == "high"
	assert "projects" in secondaries


def test_detect_all_caps_secondaries():
	registry = ModuleRegistry.load()
	primary, _, secondaries = registry.detect_all(
		prompt="Sales Invoice that auto-creates a project task and logs an attendance entry and posts to a ledger",
		target_doctype="Sales Invoice",
		max_secondaries=1,
	)
	assert len(secondaries) <= 1


def test_detect_all_dedups_primary_from_secondaries():
	registry = ModuleRegistry.load()
	primary, _, secondaries = registry.detect_all(
		prompt="Sales Invoice with accounting impact via general ledger posting",
		target_doctype="Sales Invoice",
	)
	assert primary == "accounts"
	assert "accounts" not in secondaries


def test_detect_all_no_match_returns_empty():
	registry = ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt="hello goodbye",
		target_doctype=None,
	)
	assert primary is None
	assert confidence == ""
	assert secondaries == []


def test_detect_all_primary_via_keyword_plus_secondary():
	registry = ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt="create a journal entry that logs a leave application detail",
		target_doctype=None,
	)
	assert primary == "accounts"
	assert confidence == "medium"
	assert "hr" in secondaries


@pytest.mark.parametrize("phrase,expected_module", [
	("show me leave applications by department", "hr"),
	("track attendance for a shift type", "hr"),
	("add a warehouse-specific validation", "stock"),
	("build a stock entry preview dashboard", "stock"),
	("customize the sales order form", "selling"),
	("add a sales taxes rule", "selling"),
	("create a supplier scorecard doctype", "buying"),
	("make a purchase order review helper", "buying"),
	("add a work order dashboard", "manufacturing"),
	("customize the bill of materials form", "manufacturing"),
	("add a production plan summary", "manufacturing"),
	("build a project task report", "projects"),
	("track billable hours from timesheet", "projects"),
	("customize the activity type list", "projects"),
	("add a depreciation schedule preview", "assets"),
	("asset movement report by location", "assets"),
	("track useful life per asset category", "assets"),
	("lead source analytics dashboard", "crm"),
	("opportunity stage probability chart", "crm"),
	("contract template workflow", "crm"),
	("add a salary slip preview", "payroll"),
	("customize the salary structure form", "payroll"),
	("income tax slab bracket helper", "payroll"),
])
def test_keyword_detection_across_modules(phrase, expected_module):
	registry = ModuleRegistry.load()
	module_key, confidence = registry.detect(prompt=phrase, target_doctype=None)
	assert module_key == expected_module
	assert confidence == "medium"
