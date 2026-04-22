# DocType Builder Specialist — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert a per-intent DocType Builder specialist at the final builder stage of Alfred's Dev mode, gated by `ALFRED_PER_INTENT_BUILDERS=1`. Extend `ChangesetItem` with `field_defaults_meta` so the client can render defaults as editable pills. Add an intent classifier + a defaults backfill post-processor as safety nets.

**Architecture:** New pipeline phase `_phase_classify_intent` runs after mode classification when `mode == "dev"`. When `intent == "create_doctype"` and the feature flag is on, `build_alfred_crew()` swaps the generic Developer agent for a DocType Builder specialist that reads the registry at `alfred/registry/intents/create_doctype.json`. After the crew runs, a post-processor fills any missing registry fields from defaults and annotates `field_defaults_meta`. The client reads `field_defaults_meta` and renders defaulted rows with a "default" pill + rationale tooltip.

**Tech Stack:** Python 3.11, FastAPI, CrewAI==0.203.2, pydantic v2, pytest with `asyncio_mode=auto`. Alfred uses **tabs** for indentation, line length 110, double-quote strings. Tests live under `tests/` at the repo root (sibling of `alfred/`).

**Repo root:** `/Users/navin/office/frappe_bench/v16/mariadb/alfred-processing`
**Venv:** `.venv/bin/python` (Python 3.11)
**Run tests:** `.venv/bin/python -m pytest tests/<file> -v`

**Spec reference:** `docs/specs/2026-04-21-doctype-builder-specialist.md`

---

## File Structure

**New files (alfred-processing):**
- `alfred/registry/__init__.py`
- `alfred/registry/loader.py` — `IntentRegistry` singleton + `UnknownIntentError`
- `alfred/registry/intents/_meta_schema.json` — JSON Schema for intent files
- `alfred/registry/intents/create_doctype.json` — DocType field registry
- `alfred/agents/builders/__init__.py`
- `alfred/agents/builders/doctype_builder.py` — specialist agent + task builders
- `alfred/handlers/post_build/__init__.py`
- `alfred/handlers/post_build/backfill_defaults.py` — fill missing registry fields, annotate `field_defaults_meta`
- `tests/test_registry_meta_schema.py`
- `tests/test_registry_loader.py`
- `tests/test_classify_intent.py`
- `tests/test_changeset_field_defaults_meta.py`
- `tests/test_doctype_builder.py`
- `tests/test_backfill_defaults.py`
- `tests/test_pipeline_specialist_dispatch.py`
- `tests/test_e2e_doctype_builder.py`

**Modified files (alfred-processing):**
- `pyproject.toml` — add `jsonschema` to `[project.optional-dependencies].dev`
- `alfred/models/agent_outputs.py` — add `FieldMeta`, extend `ChangesetItem` with optional `field_defaults_meta`
- `alfred/orchestrator.py` — add `IntentDecision` model + `classify_intent()` function
- `alfred/agents/crew.py` — `build_alfred_crew()` accepts `intent` kwarg; swaps Developer when `intent == "create_doctype"` and flag on
- `alfred/api/pipeline.py` — insert `_phase_classify_intent` between classify-mode and build-crew phases for dev mode; call backfill post-processor before emitting changeset

**Modified files (alfred_client):** Task 10 locates the exact Vue component; path is not known at plan-writing time.

---

## Task 1: Registry meta-schema + install jsonschema

**Files:**
- Create: `alfred/registry/__init__.py` (empty package marker)
- Create: `alfred/registry/intents/_meta_schema.json`
- Create: `tests/test_registry_meta_schema.py`
- Modify: `pyproject.toml` (add `jsonschema` to dev deps)

- [ ] **Step 1: Add jsonschema to dev deps**

Edit `pyproject.toml`. Locate the `[project.optional-dependencies]` block and the `dev = [` list. Add `"jsonschema>=4.0"` as a new entry:

```toml
dev = [
	"pytest>=8.0",
	"pytest-asyncio>=0.23",
	"httpx>=0.25",
	"ruff>=0.4",
	"jsonschema>=4.0",
]
```

Then install: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/pip install -e ".[dev]" --quiet`

- [ ] **Step 2: Create package marker**

Create `alfred/registry/__init__.py` as an empty file (one blank line).

- [ ] **Step 3: Create the meta-schema**

Create `alfred/registry/intents/_meta_schema.json`:

```json
{
	"$schema": "http://json-schema.org/draft-07/schema#",
	"title": "Alfred Intent Schema",
	"type": "object",
	"required": ["intent", "display_name", "doctype", "fields"],
	"additionalProperties": false,
	"properties": {
		"intent": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
		"display_name": {"type": "string", "minLength": 1},
		"doctype": {"type": "string", "minLength": 1},
		"fields": {
			"type": "array",
			"minItems": 1,
			"items": {
				"type": "object",
				"required": ["key", "label", "type"],
				"additionalProperties": false,
				"properties": {
					"key": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
					"label": {"type": "string", "minLength": 1},
					"type": {"enum": ["data", "check", "select", "link", "table", "int", "text"]},
					"link_doctype": {"type": "string"},
					"options": {"type": "array", "items": {"type": "string"}},
					"required": {"type": "boolean"},
					"default": {},
					"rationale": {"type": "string", "minLength": 1}
				},
				"allOf": [
					{"if": {"properties": {"type": {"const": "select"}}}, "then": {"required": ["options"]}},
					{"if": {"properties": {"type": {"const": "link"}}}, "then": {"required": ["link_doctype"]}},
					{"if": {"not": {"properties": {"required": {"const": true}}}}, "then": {"required": ["default", "rationale"]}}
				]
			}
		}
	}
}
```

- [ ] **Step 4: Write the validation test**

Create `tests/test_registry_meta_schema.py`:

```python
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
```

- [ ] **Step 5: Run the test**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_registry_meta_schema.py -v`

Expected: `test_meta_schema_is_valid_draft_07` passes. The parametrized test collects zero cases (no registry files yet) — that is expected.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add pyproject.toml alfred/registry/__init__.py alfred/registry/intents/_meta_schema.json tests/test_registry_meta_schema.py
git commit -m "feat(registry): add meta-schema for intent registry files"
```

---

## Task 2: create_doctype registry file

**Files:**
- Create: `alfred/registry/intents/create_doctype.json`

Parametrized test from Task 1 validates it automatically.

- [ ] **Step 1: Create the registry file**

Create `alfred/registry/intents/create_doctype.json`:

```json
{
	"intent": "create_doctype",
	"display_name": "Create DocType",
	"doctype": "DocType",
	"fields": [
		{
			"key": "module",
			"label": "Module",
			"type": "link",
			"link_doctype": "Module Def",
			"required": true
		},
		{
			"key": "is_submittable",
			"label": "Submittable?",
			"type": "check",
			"default": 0,
			"rationale": "Most DocTypes are not submittable. Enable only for documents with a draft / submitted / cancelled lifecycle."
		},
		{
			"key": "autoname",
			"label": "Naming rule",
			"type": "select",
			"options": ["autoincrement", "field:title", "format:PREFIX-.####", "prompt", "hash"],
			"default": "autoincrement",
			"rationale": "Autoincrement is safe when naming intent is unclear. Change if users should see meaningful IDs."
		},
		{
			"key": "istable",
			"label": "Child table?",
			"type": "check",
			"default": 0,
			"rationale": "Child tables only exist inside a parent DocType. Enable only for repeating rows."
		},
		{
			"key": "issingle",
			"label": "Singleton?",
			"type": "check",
			"default": 0,
			"rationale": "Single DocTypes store exactly one record (e.g. settings). Enable for config-style documents."
		},
		{
			"key": "permissions",
			"label": "Permissions",
			"type": "table",
			"default": [
				{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}
			],
			"rationale": "System Manager full access is the minimum usable default. Add role rows for end users."
		}
	]
}
```

- [ ] **Step 2: Run meta-schema validation**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_registry_meta_schema.py -v`

Expected: both `test_meta_schema_is_valid_draft_07` and `test_registry_file_validates_against_meta_schema[create_doctype.json]` pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/registry/intents/create_doctype.json
git commit -m "feat(registry): add create_doctype intent schema"
```

---

## Task 3: Registry loader

**Files:**
- Create: `alfred/registry/loader.py`
- Create: `tests/test_registry_loader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry_loader.py`:

```python
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
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_registry_loader.py -v`

Expected: collection error — `alfred.registry.loader` does not exist.

- [ ] **Step 3: Implement the loader**

Create `alfred/registry/loader.py`:

```python
"""Load intent schema JSON files and cache them in memory.

The registry is a set of JSON files under ``alfred/registry/intents/`` that
declare, per intent, the shape-defining fields a Builder specialist must
populate in a ``ChangesetItem``'s ``data`` dict. See the spec at
``docs/specs/2026-04-21-doctype-builder-specialist.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

SCHEMA_DIR = Path(__file__).parent / "intents"


class UnknownIntentError(KeyError):
	"""Raised when an intent key is not in the registry."""


class IntentRegistry:
	_instance: ClassVar["IntentRegistry | None"] = None

	def __init__(self, schemas: dict[str, dict]):
		self._by_intent = schemas
		self._by_doctype = {s["doctype"]: s for s in schemas.values()}

	@classmethod
	def load(cls) -> "IntentRegistry":
		if cls._instance is not None:
			return cls._instance
		schemas: dict[str, dict] = {}
		for path in SCHEMA_DIR.glob("*.json"):
			if path.name.startswith("_"):
				continue
			data = json.loads(path.read_text())
			schemas[data["intent"]] = data
		cls._instance = cls(schemas)
		return cls._instance

	def intents(self) -> list[str]:
		return sorted(self._by_intent.keys())

	def get(self, intent: str) -> dict:
		if intent not in self._by_intent:
			raise UnknownIntentError(intent)
		return self._by_intent[intent]

	def for_doctype(self, doctype: str) -> dict | None:
		"""Look up the registry entry whose ``doctype`` field matches.

		Used by the backfill post-processor to find the right intent for a
		``ChangesetItem`` after the crew has already produced it.
		"""
		return self._by_doctype.get(doctype)
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_registry_loader.py -v`

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/registry/loader.py tests/test_registry_loader.py
git commit -m "feat(registry): add cached IntentRegistry loader"
```

---

## Task 4: IntentDecision model + classify_intent

**Files:**
- Modify: `alfred/orchestrator.py` (add `IntentDecision` dataclass/model + `classify_intent` function)
- Create: `tests/test_classify_intent.py`

- [ ] **Step 1: Inspect existing `ModeDecision` for style**

Run: `grep -n "class ModeDecision\|def classify_mode" /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing/alfred/orchestrator.py`

Note the class shape (BaseModel vs dataclass) and mirror it for `IntentDecision`.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_classify_intent.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from alfred.orchestrator import IntentDecision, classify_intent


@pytest.mark.asyncio
async def test_heuristic_matches_create_doctype():
	decision = await classify_intent(
		"Create a DocType called Book with title, author, and ISBN fields",
		site_config={},
	)
	assert decision.intent == "create_doctype"
	assert decision.source == "heuristic"
	assert decision.confidence >= 0.9


@pytest.mark.asyncio
async def test_heuristic_matches_new_doctype():
	decision = await classify_intent("new doctype Employee", site_config={})
	assert decision.intent == "create_doctype"
	assert decision.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_miss_calls_classifier():
	with patch("alfred.orchestrator._classify_intent_llm", new=AsyncMock(return_value="create_doctype")) as llm:
		decision = await classify_intent(
			"I need some kind of structured thing for books maybe",
			site_config={"llm_tier": "triage"},
		)
		llm.assert_awaited_once()
		assert decision.intent == "create_doctype"
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_classifier_returns_unknown_on_no_match():
	with patch("alfred.orchestrator._classify_intent_llm", new=AsyncMock(return_value="unknown")):
		decision = await classify_intent("absolutely nothing useful here", site_config={})
		assert decision.intent == "unknown"


@pytest.mark.asyncio
async def test_classifier_failure_falls_back_to_unknown():
	with patch("alfred.orchestrator._classify_intent_llm", new=AsyncMock(side_effect=RuntimeError("boom"))):
		decision = await classify_intent("totally unclear prompt", site_config={})
		assert decision.intent == "unknown"
		assert decision.source == "fallback"
```

- [ ] **Step 3: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_classify_intent.py -v`

Expected: import error — `classify_intent` / `IntentDecision` not in `alfred.orchestrator`.

- [ ] **Step 4: Implement IntentDecision + classify_intent**

Append to `alfred/orchestrator.py` (at the bottom of the file, tab indent, keeping Alfred's existing style):

```python
# ── Intent classification (Dev mode) ─────────────────────────────
# Runs only for dev-mode prompts to pick a per-intent Builder specialist.
# Mirrors classify_mode(): heuristic first, LLM fallback, "unknown" on
# failure. Spec: docs/specs/2026-04-21-doctype-builder-specialist.md

from pydantic import BaseModel, Field as _PydField
import logging as _intent_logging

_intent_logger = _intent_logging.getLogger("alfred.orchestrator.intent")

_SUPPORTED_INTENTS: tuple[str, ...] = ("create_doctype",)

_HEURISTIC_PATTERNS: dict[str, tuple[str, ...]] = {
	"create_doctype": (
		"create a doctype",
		"create doctype",
		"new doctype",
		"add a doctype",
		"add doctype",
		"build a doctype",
		"make a doctype",
	),
}


class IntentDecision(BaseModel):
	intent: str = _PydField(..., description="Intent key or 'unknown'")
	confidence: float = _PydField(0.0, ge=0.0, le=1.0)
	source: str = _PydField("fallback", description="heuristic | classifier | fallback")
	reason: str = ""


def _match_intent_heuristic(prompt: str) -> str | None:
	low = prompt.lower()
	for intent, patterns in _HEURISTIC_PATTERNS.items():
		if any(p in low for p in patterns):
			return intent
	return None


async def _classify_intent_llm(prompt: str, site_config: dict) -> str:
	"""Small LLM call that returns one of the supported intents or 'unknown'.

	Kept as a module-level function so tests can patch it without touching
	the rest of the orchestrator.
	"""
	from alfred.llm_client import ollama_chat

	system = (
		"You classify the user's Frappe customization request into ONE intent. "
		f"Valid intents: {', '.join(_SUPPORTED_INTENTS)}, unknown. "
		"Reply with ONLY the intent key, no prose, no punctuation."
	)
	reply = await ollama_chat(
		messages=[
			{"role": "system", "content": system},
			{"role": "user", "content": prompt},
		],
		site_config=site_config,
		tier=site_config.get("llm_tier", "triage"),
		max_tokens=16,
		temperature=0.0,
	)
	tag = (reply or "").strip().lower()
	return tag if tag in (*_SUPPORTED_INTENTS, "unknown") else "unknown"


async def classify_intent(prompt: str, site_config: dict) -> IntentDecision:
	heur = _match_intent_heuristic(prompt)
	if heur is not None:
		return IntentDecision(
			intent=heur, confidence=0.95, source="heuristic",
			reason=f"matched heuristic pattern for {heur}",
		)

	try:
		tag = await _classify_intent_llm(prompt, site_config)
		return IntentDecision(
			intent=tag,
			confidence=0.7 if tag != "unknown" else 0.3,
			source="classifier",
			reason=f"LLM classifier returned {tag}",
		)
	except Exception as e:
		_intent_logger.warning("Intent classifier failed: %s", e)
		return IntentDecision(
			intent="unknown", confidence=0.0, source="fallback",
			reason=f"classifier error: {e}",
		)
```

- [ ] **Step 5: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_classify_intent.py -v`

Expected: all 5 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/orchestrator.py tests/test_classify_intent.py
git commit -m "feat(orchestrator): add dev-mode intent classifier"
```

---

## Task 5: Extend ChangesetItem with field_defaults_meta

**Files:**
- Modify: `alfred/models/agent_outputs.py`
- Create: `tests/test_changeset_field_defaults_meta.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_changeset_field_defaults_meta.py`:

```python
from alfred.models.agent_outputs import ChangesetItem, FieldMeta


def test_field_defaults_meta_defaults_to_none():
	item = ChangesetItem(operation="create", doctype="DocType", data={"name": "Book"})
	assert item.field_defaults_meta is None


def test_field_defaults_meta_accepts_dict():
	item = ChangesetItem(
		operation="create",
		doctype="DocType",
		data={"autoname": "autoincrement"},
		field_defaults_meta={"autoname": FieldMeta(source="default", rationale="Safe default.")},
	)
	assert item.field_defaults_meta is not None
	assert item.field_defaults_meta["autoname"].source == "default"
	assert item.field_defaults_meta["autoname"].rationale == "Safe default."


def test_field_meta_source_user_allows_null_rationale():
	meta = FieldMeta(source="user")
	assert meta.source == "user"
	assert meta.rationale is None


def test_field_meta_source_must_be_user_or_default():
	import pytest
	from pydantic import ValidationError

	with pytest.raises(ValidationError):
		FieldMeta(source="invalid")


def test_serialization_round_trip():
	item = ChangesetItem(
		operation="create",
		doctype="DocType",
		data={"name": "Book"},
		field_defaults_meta={"autoname": FieldMeta(source="default", rationale="r")},
	)
	dumped = item.model_dump()
	restored = ChangesetItem.model_validate(dumped)
	assert restored.field_defaults_meta["autoname"].source == "default"
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_changeset_field_defaults_meta.py -v`

Expected: `FieldMeta` not importable from `alfred.models.agent_outputs`.

- [ ] **Step 3: Extend agent_outputs.py**

Open `alfred/models/agent_outputs.py`. Locate the `ChangesetItem` class (the Explore report put it around line 164). At the top of the file (near the other pydantic imports), ensure `Literal` and `Optional` are imported:

```python
from typing import Any, Literal, Optional  # augment existing imports if needed
```

Above `class ChangesetItem`, add:

```python
class FieldMeta(BaseModel):
	"""Provenance annotation for a single key inside ``ChangesetItem.data``.

	Written by the per-intent Builder specialist and by the defaults
	backfill post-processor. Consumed by ``alfred_client`` to render
	default rows with a "default" pill and rationale tooltip. Server-side
	Frappe deploy ignores this field.
	"""

	source: Literal["user", "default"]
	rationale: Optional[str] = None
```

Then in `class ChangesetItem`, add a new field (after `data`):

```python
	field_defaults_meta: Optional[dict[str, FieldMeta]] = None
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_changeset_field_defaults_meta.py -v`

Expected: all 5 tests pass.

- [ ] **Step 5: Run the full test suite to check no regressions**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest -x -q`

Expected: no regressions in pre-existing tests. If any test previously asserted a fixed shape for `ChangesetItem.model_dump()`, it may now include `"field_defaults_meta": None`. Update those assertions to use `.model_dump(exclude_none=True)` or add `"field_defaults_meta": None` to the expected dict.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/models/agent_outputs.py tests/test_changeset_field_defaults_meta.py
git commit -m "feat(models): add FieldMeta and ChangesetItem.field_defaults_meta"
```

---

## Task 6: DocType Builder specialist

**Files:**
- Create: `alfred/agents/builders/__init__.py`
- Create: `alfred/agents/builders/doctype_builder.py`
- Create: `tests/test_doctype_builder.py`

- [ ] **Step 1: Inspect current Developer agent + generate_changeset task**

Run: `grep -n "generate_changeset\|developer" /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing/alfred/agents/crew.py | head -40`

Read the relevant function and task description so the specialist can reuse the base prompt structure. Record which function(s) construct today's Developer agent and `generate_changeset` task.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_doctype_builder.py`:

```python
from alfred.agents.builders.doctype_builder import (
	render_registry_checklist,
	build_doctype_builder_agent,
	build_doctype_builder_task,
)
from alfred.registry.loader import IntentRegistry


def test_render_registry_checklist_lists_every_field():
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema)
	for key in ("module", "is_submittable", "autoname", "istable", "issingle", "permissions"):
		assert key in text


def test_render_registry_checklist_mentions_field_defaults_meta():
	schema = IntentRegistry.load().get("create_doctype")
	text = render_registry_checklist(schema)
	assert "field_defaults_meta" in text


def test_build_doctype_builder_agent_returns_agent_with_doctype_backstory():
	agent = build_doctype_builder_agent(site_config={}, custom_tools=None)
	assert "DocType" in agent.backstory
	assert "specialis" in agent.backstory.lower()  # "specialise" / "specialize"


def test_build_doctype_builder_task_includes_registry_text():
	agent = build_doctype_builder_agent(site_config={}, custom_tools=None)
	task = build_doctype_builder_task(agent)
	assert "autoname" in task.description
	assert "field_defaults_meta" in task.description
```

- [ ] **Step 3: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_doctype_builder.py -v`

Expected: module not found.

- [ ] **Step 4: Create the builders package marker**

Create `alfred/agents/builders/__init__.py` with one blank line.

- [ ] **Step 5: Implement the DocType Builder**

Create `alfred/agents/builders/doctype_builder.py`:

```python
"""DocType Builder specialist — a specialist variant of the Developer agent.

Selected by ``build_alfred_crew`` when ``intent == "create_doctype"`` and
``ALFRED_PER_INTENT_BUILDERS=1``. Reads shape-defining fields from the
registry at ``alfred/registry/intents/create_doctype.json`` and appends a
checklist to the Developer's task description so every DocType emitted
includes ``module``, ``is_submittable``, ``autoname``, ``istable``,
``issingle``, and ``permissions``.

Spec: ``docs/specs/2026-04-21-doctype-builder-specialist.md``
"""

from __future__ import annotations

from crewai import Agent, Task

from alfred.registry.loader import IntentRegistry

_DOCTYPE_BACKSTORY_ADDENDUM = """
You specialise in creating Frappe DocTypes. You know the distinction between \
submittable documents (draft / submitted / cancelled lifecycle) and non-submittable \
documents; between autoincrement, field-based naming, format strings with series, \
prompt, and hash naming; between parent DocTypes, child tables, and singletons; and \
the minimum permission set required for a usable DocType. Every DocType you emit \
MUST include `module`, `is_submittable`, `autoname`, `istable`, `issingle`, and at \
least one `permissions` row in its `data`. If the user did not specify a value, use \
the registry default and record which fields were defaulted in `field_defaults_meta`.
""".strip()


def render_registry_checklist(schema: dict) -> str:
	lines = [
		"SHAPE-DEFINING FIELDS for create_doctype (you MUST include every one of these in `data`):",
	]
	for field in schema["fields"]:
		key = field["key"]
		if field.get("required"):
			lines.append(f"  - {key} (required, user-provided; if missing, leave as empty string)")
		else:
			default_str = repr(field["default"])
			lines.append(f"  - {key} (default {default_str})")
	lines.append("")
	lines.append(
		"Additionally, emit a parallel `field_defaults_meta` dict on the changeset "
		"item. For each of the shape-defining fields above, record whether you took "
		"the value from the user or from the registry default, and include the "
		"registry rationale when defaulted. Example:"
	)
	lines.append(
		'  "field_defaults_meta": {'
		'"is_submittable": {"source": "default", "rationale": "..."}, '
		'"module": {"source": "user"}}'
	)
	return "\n".join(lines)


def build_doctype_builder_agent(site_config: dict, custom_tools: dict | None) -> Agent:
	"""Build a CrewAI Agent that is a DocType specialist variant of the Developer.

	The base role + goal stay close to today's generic Developer so the crew
	pipeline keeps working; only the backstory is extended with DocType
	expertise. Tools are passed through from ``custom_tools`` (the MCP tool
	map today's Developer uses).
	"""
	tools = []
	if custom_tools:
		for key in ("lookup_doctype", "lookup_pattern", "lookup_frappe_knowledge", "get_site_customization_detail"):
			t = custom_tools.get(key)
			if t is not None:
				tools.append(t)

	return Agent(
		role="Frappe Developer — DocType Specialist",
		goal=(
			"Generate a production-ready DocType changeset item whose `data` "
			"includes every shape-defining field from the registry, with "
			"`field_defaults_meta` describing which fields were defaulted."
		),
		backstory=_DOCTYPE_BACKSTORY_ADDENDUM,
		allow_delegation=False,
		tools=tools,
		verbose=False,
	)


def build_doctype_builder_task(agent: Agent) -> Task:
	"""Build the generate_changeset Task for the DocType specialist.

	The description appends a registry-rendered checklist to the base
	generate_changeset prompt. The base JSON-output rules are left to the
	caller that wires this into the crew (crew.py does the final composition).
	"""
	schema = IntentRegistry.load().get("create_doctype")
	checklist = render_registry_checklist(schema)

	description = (
		"Produce a Frappe changeset for the user's DocType request. "
		"OUTPUT FORMAT (STRICT): Your entire Final Answer MUST be a single "
		"JSON array. Each item has keys: operation ('create'|'update'|'delete'), "
		"doctype (e.g. 'DocType'), data (object with the full DocType definition), "
		"and optional field_defaults_meta (object keyed by field name).\n\n"
		+ checklist
	)

	return Task(
		description=description,
		agent=agent,
		expected_output="JSON array of changeset items with field_defaults_meta",
	)
```

- [ ] **Step 6: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_doctype_builder.py -v`

Expected: all 4 tests pass.

- [ ] **Step 7: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/builders/__init__.py alfred/agents/builders/doctype_builder.py tests/test_doctype_builder.py
git commit -m "feat(agents): add DocType Builder specialist"
```

---

## Task 7: Defaults backfill post-processor

**Files:**
- Create: `alfred/handlers/post_build/__init__.py`
- Create: `alfred/handlers/post_build/backfill_defaults.py`
- Create: `tests/test_backfill_defaults.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backfill_defaults.py`:

```python
from alfred.handlers.post_build.backfill_defaults import backfill_defaults
from alfred.models.agent_outputs import Changeset, ChangesetItem


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
	cs = Changeset(
		items=[ChangesetItem(operation="create", doctype="Custom Field", data={"fieldname": "x"})]
	)
	out = backfill_defaults(cs)
	assert out.items[0].data == {"fieldname": "x"}
	assert out.items[0].field_defaults_meta is None


def test_preexisting_field_defaults_meta_is_respected():
	from alfred.models.agent_outputs import FieldMeta
	item = _doctype_item({"name": "Book", "module": "Custom", "autoname": "field:title"})
	item.field_defaults_meta = {"autoname": FieldMeta(source="user")}
	cs = Changeset(items=[item])
	out = backfill_defaults(cs)
	meta = out.items[0].field_defaults_meta
	assert meta["autoname"].source == "user"
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_backfill_defaults.py -v`

Expected: module not found.

- [ ] **Step 3: Create package marker**

Create `alfred/handlers/post_build/__init__.py` with one blank line.

- [ ] **Step 4: Implement the post-processor**

Create `alfred/handlers/post_build/backfill_defaults.py`:

```python
"""Fill missing registry fields on a Changeset before it reaches the client.

Safety net: if the Builder specialist's LLM output drops a shape-defining
field (e.g. forgets `autoname`), this post-processor inserts the registry
default and annotates ``field_defaults_meta`` so the client can still
render it as a defaulted row. User-provided values are never overwritten.

Spec: ``docs/specs/2026-04-21-doctype-builder-specialist.md``
"""

from __future__ import annotations

import copy
import logging

from alfred.models.agent_outputs import Changeset, ChangesetItem, FieldMeta
from alfred.registry.loader import IntentRegistry

logger = logging.getLogger("alfred.handlers.post_build.backfill")


def backfill_defaults(changeset: Changeset) -> Changeset:
	"""Return a new Changeset with registry fields backfilled and annotated."""
	registry = IntentRegistry.load()
	new_items: list[ChangesetItem] = []
	for item in changeset.items:
		schema = registry.for_doctype(item.doctype)
		if schema is None:
			new_items.append(item)
			continue
		new_items.append(_backfill_item(item, schema))
	return Changeset(items=new_items)


def _backfill_item(item: ChangesetItem, schema: dict) -> ChangesetItem:
	data = copy.deepcopy(item.data)
	meta: dict[str, FieldMeta] = dict(item.field_defaults_meta or {})

	for field in schema["fields"]:
		key = field["key"]
		if key in data and data[key] not in (None, ""):
			if key not in meta:
				meta[key] = FieldMeta(source="user")
			continue
		if "default" not in field:
			# Required field with no default; leave absent/empty and record as user-sourced
			if key not in meta:
				meta[key] = FieldMeta(source="user")
			continue
		data[key] = copy.deepcopy(field["default"])
		if key not in meta:
			meta[key] = FieldMeta(source="default", rationale=field.get("rationale"))

	return ChangesetItem(
		operation=item.operation,
		doctype=item.doctype,
		data=data,
		field_defaults_meta=meta,
	)
```

- [ ] **Step 5: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_backfill_defaults.py -v`

Expected: all 5 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/handlers/post_build/ tests/test_backfill_defaults.py
git commit -m "feat(handlers): add defaults backfill post-processor"
```

---

## Task 8: Specialist dispatch in `build_alfred_crew`

**Files:**
- Modify: `alfred/agents/crew.py`
- Create: `tests/test_crew_specialist_dispatch.py`

- [ ] **Step 1: Inspect crew.py's Developer-building path**

Run: `grep -n "def build_alfred_crew\|developer\|generate_changeset" /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing/alfred/agents/crew.py | head -30`

Record: (a) the signature of `build_alfred_crew`, (b) the line where the Developer agent is built, (c) the line where the `generate_changeset` task is built. You will insert conditional swaps at (b) and (c).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_crew_specialist_dispatch.py`:

```python
import os
from unittest.mock import patch

import pytest

from alfred.agents.crew import _pick_developer_agent_and_task, _per_intent_builders_enabled


def test_flag_off_returns_none():
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		assert _per_intent_builders_enabled() is False


def test_flag_on_returns_true():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		assert _per_intent_builders_enabled() is True


def test_pick_with_flag_off_returns_none():
	with patch.dict(os.environ, {}, clear=False):
		os.environ.pop("ALFRED_PER_INTENT_BUILDERS", None)
		result = _pick_developer_agent_and_task(intent="create_doctype", site_config={}, custom_tools=None)
		assert result is None


def test_pick_with_unknown_intent_returns_none():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		result = _pick_developer_agent_and_task(intent="unknown", site_config={}, custom_tools=None)
		assert result is None


def test_pick_with_create_doctype_returns_specialist_pair():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		result = _pick_developer_agent_and_task(intent="create_doctype", site_config={}, custom_tools=None)
		assert result is not None
		agent, task = result
		assert "DocType" in agent.role
		assert "field_defaults_meta" in task.description
```

- [ ] **Step 3: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_crew_specialist_dispatch.py -v`

Expected: `_pick_developer_agent_and_task` / `_per_intent_builders_enabled` not in `alfred.agents.crew`.

- [ ] **Step 4: Add dispatch helpers to crew.py**

At the bottom of `alfred/agents/crew.py`, append:

```python
# ── Per-intent Builder specialist dispatch ───────────────────────
# See docs/specs/2026-04-21-doctype-builder-specialist.md.
# Feature-flagged via ALFRED_PER_INTENT_BUILDERS=1. When off or when
# intent is unknown/None, callers stick with the generic Developer +
# generate_changeset task defined elsewhere in this module.

import os as _os
from crewai import Agent as _Agent, Task as _Task


def _per_intent_builders_enabled() -> bool:
	return _os.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1"


def _pick_developer_agent_and_task(
	*,
	intent: str | None,
	site_config: dict,
	custom_tools: dict | None,
) -> tuple[_Agent, _Task] | None:
	"""Return (agent, task) for the per-intent specialist, or None to stick with the generic Developer.

	Returns None when the feature flag is off, when ``intent`` is None or
	"unknown", or when no specialist is registered for the intent. Callers
	must fall back to the generic Developer + generate_changeset task when
	None is returned.
	"""
	if not _per_intent_builders_enabled():
		return None
	if not intent or intent == "unknown":
		return None

	if intent == "create_doctype":
		from alfred.agents.builders.doctype_builder import (
			build_doctype_builder_agent,
			build_doctype_builder_task,
		)
		agent = build_doctype_builder_agent(site_config=site_config, custom_tools=custom_tools)
		task = build_doctype_builder_task(agent)
		return agent, task

	return None
```

- [ ] **Step 5: Wire dispatch into `build_alfred_crew`**

Modify the function signature of `build_alfred_crew` to accept an optional `intent` kwarg (default `None`). At the point where the Developer agent and the `generate_changeset` task are constructed today (locate them from Step 1), replace the direct construction with a branch:

```python
# Pseudocode — adapt to the exact variable names in crew.py. Keep tabs.
specialist = _pick_developer_agent_and_task(
	intent=intent,
	site_config=site_config,
	custom_tools=custom_tools,
)
if specialist is not None:
	developer_agent, generate_changeset_task = specialist
else:
	# existing construction of the generic Developer + generate_changeset task
	developer_agent = ...  # unchanged existing code
	generate_changeset_task = ...  # unchanged existing code
```

Leave all other agents and tasks (Requirement, Assessment, Architect, Tester, Deployer) untouched.

- [ ] **Step 6: Run the new dispatch tests — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_crew_specialist_dispatch.py -v`

Expected: all 5 tests pass.

- [ ] **Step 7: Run the full test suite — expect no regressions**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest -x -q`

Expected: pre-existing `test_crew.py`, `test_agents.py`, etc. still pass. The dispatch is no-op when the flag is off, so behavior should be unchanged.

- [ ] **Step 8: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/crew.py tests/test_crew_specialist_dispatch.py
git commit -m "feat(crew): per-intent Builder dispatch behind ALFRED_PER_INTENT_BUILDERS flag"
```

---

## Task 9: Wire intent classifier + backfill into the dev pipeline

**Files:**
- Modify: `alfred/api/pipeline.py`
- Create: `tests/test_pipeline_specialist_integration.py`

- [ ] **Step 1: Locate the dev-mode phases**

Run: `grep -n "def _phase\|mode == .dev.\|build_alfred_crew\|_extract_changes" /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing/alfred/api/pipeline.py | head -40`

Record: (a) where `classify_mode` is called (end of `_phase_orchestrate` per the Explore report, ~line 747), (b) where `build_alfred_crew` is called, (c) where the changeset is extracted and emitted.

- [ ] **Step 2: Write the failing integration test**

Create `tests/test_pipeline_specialist_integration.py`:

```python
"""Integration-level test for the dev-mode pipeline wiring.

Uses a small bespoke driver that mirrors the phase order actually used in
``pipeline.py`` (_phase_classify_intent after mode==dev, then crew build,
then backfill). This keeps the test focused on the new wiring without
standing up the full pipeline state machine.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from alfred.models.agent_outputs import Changeset, ChangesetItem
from alfred.handlers.post_build.backfill_defaults import backfill_defaults
from alfred.orchestrator import classify_intent


@pytest.mark.asyncio
async def test_dev_prompt_classified_and_backfilled(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")

	# Stage 1: classify intent
	decision = await classify_intent(
		"Create a DocType called Book with title, author, ISBN",
		site_config={},
	)
	assert decision.intent == "create_doctype"

	# Stage 2: simulate crew output (Developer wrote partial data)
	raw_cs = Changeset(items=[
		ChangesetItem(
			operation="create",
			doctype="DocType",
			data={
				"name": "Book",
				"module": "Custom",
				"fields": [
					{"fieldname": "title", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "author", "fieldtype": "Data", "reqd": 1},
					{"fieldname": "isbn", "fieldtype": "Data", "unique": 1},
				],
			},
		),
	])

	# Stage 3: backfill
	final = backfill_defaults(raw_cs)
	item = final.items[0]
	assert item.data["autoname"] == "autoincrement"
	assert item.data["is_submittable"] == 0
	assert isinstance(item.data["permissions"], list)
	assert item.field_defaults_meta["autoname"].source == "default"
	assert item.field_defaults_meta["module"].source == "user"
	assert item.field_defaults_meta["is_submittable"].rationale
```

Run it now: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_pipeline_specialist_integration.py -v`

Expected: this test passes already — it only exercises the components from Tasks 4 and 7 — and serves as a contract for the actual pipeline wiring in the next step.

- [ ] **Step 3: Insert `_phase_classify_intent` in pipeline.py**

Open `alfred/api/pipeline.py`. After the existing `_phase_orchestrate` (or wherever `classify_mode` is called and the mode decision is stored in run state), add a new phase function — modeled on the existing `_phase_orchestrate` — that runs only when `mode == "dev"` and calls `classify_intent`, storing the `IntentDecision` in run state:

```python
async def _phase_classify_intent(state) -> None:
	"""Classify the per-intent builder target for dev mode. No-op otherwise."""
	mode_decision = state.get("mode_decision")
	if not mode_decision or mode_decision.mode != "dev":
		return
	from alfred.orchestrator import classify_intent  # lazy import to avoid cycles
	decision = await classify_intent(
		prompt=state["prompt"],
		site_config=state.get("site_config") or {},
	)
	state["intent_decision"] = decision
```

Then splice it into the phase sequence so it runs after `_phase_orchestrate` and before `_phase_build_crew`. Exact location depends on pipeline.py's sequencing code (a list of phase functions or an explicit await chain); adapt accordingly.

- [ ] **Step 4: Pass intent to `build_alfred_crew`**

Locate the call to `build_alfred_crew(...)` in `pipeline.py`. Pass `intent=state.get("intent_decision").intent if state.get("intent_decision") else None`.

- [ ] **Step 5: Run backfill after the crew emits the changeset**

Locate the site where `_extract_changes()` produces the final `Changeset` object handed to the WebSocket. Insert a call:

```python
from alfred.handlers.post_build.backfill_defaults import backfill_defaults
changeset = backfill_defaults(changeset)
```

Place this after extraction and before the WebSocket emission. Gate it on the feature flag if you want strict parity with flag-off behavior:

```python
if os.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1":
	changeset = backfill_defaults(changeset)
```

- [ ] **Step 6: Run tests — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_pipeline_specialist_integration.py -v`

Expected: passes. Also run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest -x -q` for no regressions.

- [ ] **Step 7: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/api/pipeline.py tests/test_pipeline_specialist_integration.py
git commit -m "feat(pipeline): dispatch per-intent specialist and backfill defaults"
```

---

## Task 10: `alfred_client` changeset review UI — render field_defaults_meta

**Files:**
- Locate and modify the Vue component rendering changeset review
- Locate and modify any changeset pydantic/JS type definitions in `alfred_client/` that assume the old shape

Alfred's client is at `/Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client/`.

- [ ] **Step 1: Locate the changeset review component**

Run these commands and record which files render the changeset:

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
grep -rn "changeset\|Changeset" --include='*.vue' --include='*.js' --include='*.ts' . | head -30
grep -rn "field_defaults_meta\|is_submittable\|autoname" --include='*.vue' --include='*.js' --include='*.ts' . | head -30
```

Record the primary Vue component path and the data model / type file (if any).

- [ ] **Step 2: Inspect the current rendering and how `data` is iterated**

Read the primary Vue component. Note: (a) how items are iterated, (b) where `item.data` is rendered, (c) whether it uses `v-for` over `Object.keys(item.data)` today or a hard-coded field list.

- [ ] **Step 3: Add per-field rendering for DocType items with `field_defaults_meta`**

Modify the Vue component so that, for each ChangesetItem:

1. If `item.field_defaults_meta` exists: iterate `Object.keys(item.data)` and render each as an editable input.
2. For keys present in `item.field_defaults_meta`:
   - If `source == "default"`: render a `<span class="planv2-default-pill" :title="rationale">default</span>` next to the label.
   - On input change: flip the local `field_defaults_meta[key].source` to `"user"` and remove the rationale so the pill disappears (Vue reactivity).
3. If a key's current value is empty AND the registry marks it required (detected via `field_defaults_meta[key].source === 'user' && !value`): add a CSS class `planv2-required-empty` and disable the Deploy button until every required key has a value.
4. If `item.field_defaults_meta` is null or undefined: fall back to today's rendering unchanged.

Exact code depends on the component's existing structure. Preserve its idioms.

- [ ] **Step 4: Add CSS for the pill, tooltip, and required-empty outline**

Add to the component's scoped style (or the nearest shared stylesheet):

```css
.planv2-default-pill {
	background: #eef;
	color: #334;
	font-size: 11px;
	padding: 2px 6px;
	border-radius: 10px;
	cursor: help;
	margin-left: 6px;
}
.planv2-required-empty input,
.planv2-required-empty select {
	outline: 2px solid #d9534f;
}
```

- [ ] **Step 5: Update TS/JS types if they exist**

If `alfred_client/` has a TypeScript or JSDoc type for `ChangesetItem`, add the optional `field_defaults_meta` field to match the server shape:

```typescript
type FieldMeta = { source: "user" | "default"; rationale?: string | null };
type ChangesetItem = {
	operation: "create" | "update" | "delete";
	doctype: string;
	data: Record<string, unknown>;
	field_defaults_meta?: Record<string, FieldMeta> | null;
};
```

- [ ] **Step 6: Manual smoke test end-to-end**

Start alfred-processing with the flag: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && ALFRED_PER_INTENT_BUILDERS=1 ./dev.sh`

Open the alfred_client UI in Frappe Desk. Send: *"Create a DocType called Book with title, author, and ISBN fields."*

Expected:
- Changeset review UI shows a `DocType Book` card.
- Six shape-defining fields render: `module`, `is_submittable`, `autoname`, `istable`, `issingle`, `permissions`.
- Pills show on `is_submittable`, `autoname`, `istable`, `issingle`, `permissions`; hovering shows the registry rationale.
- `module` is red-outlined; Deploy is disabled.
- Typing "Custom" into `module` enables Deploy.
- Clicking Deploy creates the DocType in Frappe with `autoname=autoincrement`, `is_submittable=0`, a System Manager permission row.

Also re-run with flag unset to confirm no UI regression: changeset renders today's way.

- [ ] **Step 7: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
git add .
git commit -m "feat(client): render field_defaults_meta with editable default pills"
```

---

## Self-Review

**Spec coverage:**
- A. Intent schema registry → Tasks 1, 2, 3
- B. Intent classifier phase → Tasks 4, 9
- C. DocType Builder specialist → Task 6
- D. ChangesetItem extension → Task 5
- E. Specialist dispatch in build_alfred_crew → Task 8
- F. Defaults backfill post-processor → Task 7
- G. alfred_client review UI → Task 10
- Data flow → Tasks 8, 9, 10
- Error handling (unknown intent, classifier failure) → Task 4 (`_classify_intent_llm` exception path, `unknown` fallback)
- Error handling (missing registry for a doctype) → Task 7 (pass-through, `field_defaults_meta: None`)
- Error handling (feature flag off) → Tasks 8, 9 (no-op paths)
- Testing — Tasks 1-9 each have pytest coverage; Task 10 covers client-side via manual E2E (Vue component tests depend on tooling presence in alfred_client, flagged optional in the task).
- Rollout via `ALFRED_PER_INTENT_BUILDERS` flag → Tasks 8, 9
- Follow-on specialists (non-V1) → not scheduled in this plan by design (spec declares V1 = DocType only).

**Placeholder scan:** no "TBD", "TODO", "implement later". Two controlled ambiguities are called out explicitly with grep commands that find the actual paths:
- Task 8: location of Developer agent construction in `crew.py`
- Task 9: phase sequencing insertion point in `pipeline.py`
- Task 10: Vue component path in `alfred_client/`

**Type consistency:** `IntentDecision`, `IntentRegistry`, `UnknownIntentError`, `FieldMeta`, `ChangesetItem.field_defaults_meta`, `build_doctype_builder_agent`, `build_doctype_builder_task`, `render_registry_checklist`, `_pick_developer_agent_and_task`, `_per_intent_builders_enabled`, `backfill_defaults` — referenced consistently across tasks.
