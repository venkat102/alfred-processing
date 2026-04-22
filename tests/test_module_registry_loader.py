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
	assert registry.for_doctype("Employee") is None


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
		prompt="employee onboarding flow",
		target_doctype=None,
	)
	assert module_key is None
	assert confidence is None
