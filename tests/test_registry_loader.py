import pytest

from alfred.registry.loader import IntentRegistry, UnknownIntentError


@pytest.fixture(autouse=True)
def _reset_registry():
	IntentRegistry._instance = None
	yield
	IntentRegistry._instance = None


def test_load_returns_registry_with_known_intents():
	registry = IntentRegistry.load()
	assert "create_doctype" in registry.intents()


def test_get_returns_schema_dict():
	registry = IntentRegistry.load()
	schema = registry.get("create_doctype")
	assert schema["intent"] == "create_doctype"
	assert schema["display_name"] == "Create DocType"
	assert schema["doctype"] == "DocType"
	assert any(f["key"] == "module" for f in schema["fields"])


def test_get_unknown_intent_raises():
	registry = IntentRegistry.load()
	with pytest.raises(UnknownIntentError):
		registry.get("not_a_real_intent")


def test_load_returns_singleton():
	first = IntentRegistry.load()
	second = IntentRegistry.load()
	assert first is second


def test_for_doctype_matches_registry_doctype():
	registry = IntentRegistry.load()
	schema = registry.for_doctype("DocType")
	assert schema is not None
	assert schema["intent"] == "create_doctype"


def test_for_doctype_unknown_returns_none():
	registry = IntentRegistry.load()
	assert registry.for_doctype("Nonexistent DocType") is None
