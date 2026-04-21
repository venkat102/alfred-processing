import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "alfred" / "registry" / "intents"


@pytest.fixture(scope="module")
def meta_schema():
	return json.loads((SCHEMA_DIR / "_meta_schema.json").read_text())


def test_meta_schema_is_valid_draft_07(meta_schema):
	jsonschema.Draft7Validator.check_schema(meta_schema)


@pytest.mark.parametrize(
	"registry_path",
	[p for p in SCHEMA_DIR.glob("*.json") if p.name != "_meta_schema.json"],
	ids=lambda p: p.name,
)
def test_registry_file_validates_against_meta_schema(meta_schema, registry_path):
	data = json.loads(registry_path.read_text())
	jsonschema.validate(data, meta_schema)
