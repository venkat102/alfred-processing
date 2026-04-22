# Module Specialists (V2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add cross-cutting module specialists (one Agent per ERPNext module) that get invoked twice per Dev-mode build — once to inject domain context into the intent specialist's prompt, once to validate the output against module conventions — gated by `ALFRED_MODULE_SPECIALISTS=1` on top of V1's `ALFRED_PER_INTENT_BUILDERS=1`. Ship pilot module `accounts`.

**Architecture:** New `ModuleRegistry` loader parallel to V1's `IntentRegistry`. New `classify_module` and `provide_module_context` phases in the pipeline. Module specialist is Python-orchestrated (direct `ollama_chat` calls templated from the module KB's backstory) rather than a CrewAI crew task — keeps specialists testable in isolation and avoids inflating the crew sequence. Validation notes plumb through to the preview panel alongside existing dry-run issues.

**Tech Stack:** Python 3.11, FastAPI, CrewAI==0.203.2, pydantic v2, pytest with `asyncio_mode=auto`. Tabs for indentation, line length 110, double-quote strings. Tests under `tests/` at repo root.

**Repo root:** `/Users/navin/office/frappe_bench/v16/mariadb/alfred-processing`
**Venv:** `.venv/bin/python` (Python 3.11)
**Run tests:** `.venv/bin/python -m pytest tests/<file> -v`

**Spec reference:** `docs/specs/2026-04-22-module-specialists.md`
**V1 reference (pattern source):** `docs/specs/2026-04-21-doctype-builder-specialist.md` + its plan `docs/plans/2026-04-21-doctype-builder-specialist.md`

---

## File Structure

**New files (alfred-processing):**
- `alfred/registry/modules/_meta_schema.json` — JSON Schema for module KB files
- `alfred/registry/modules/accounts.json` — pilot module KB
- `alfred/registry/module_loader.py` — cached `ModuleRegistry` singleton
- `alfred/agents/specialists/__init__.py`
- `alfred/agents/specialists/module_specialist.py` — `provide_context` + `validate_output` async functions + deterministic rule runner
- `tests/test_module_registry_meta_schema.py`
- `tests/test_module_registry_loader.py`
- `tests/test_module_specialist_rules.py`
- `tests/test_module_specialist_llm.py`
- `tests/test_detect_module.py`
- `tests/test_backfill_module_defaults.py`
- `tests/test_pipeline_module_integration.py`

**Modified files (alfred-processing):**
- `alfred/models/agent_outputs.py` — add `ValidationNote` pydantic model
- `alfred/orchestrator.py` — add `ModuleDecision` dataclass + `detect_module()` function
- `alfred/agents/builders/doctype_builder.py` — extend `enhance_generate_changeset_description` to accept `module_context`
- `alfred/agents/crew.py` — `build_alfred_crew` and `_enhance_task_description` accept `module_context`
- `alfred/handlers/post_build/backfill_defaults.py` — extend `backfill_defaults_raw` with optional `module` kwarg
- `alfred/api/pipeline.py` — add `classify_module` + `provide_module_context` phases, extend `PipelineContext`, call module validator in `_phase_post_crew`
- `tests/test_doctype_builder.py` — extend with `module_context` tests
- `tests/test_backfill_defaults_raw.py` — extend with `module` kwarg tests

**Modified files (alfred_client):**
- `alfred_client/alfred_client/public/js/alfred_chat/PreviewPanel.vue` — render `module_validation_notes` and module badge

---

## Task 1: Module KB meta-schema + validation test

**Files:**
- Create: `alfred/registry/modules/_meta_schema.json`
- Create: `tests/test_module_registry_meta_schema.py`

- [ ] **Step 1: Create the meta-schema**

Create `alfred/registry/modules/_meta_schema.json`:

```json
{
	"$schema": "http://json-schema.org/draft-07/schema#",
	"title": "Alfred Module KB Schema",
	"type": "object",
	"required": ["module", "display_name", "frappe_module_key", "backstory", "conventions", "validation_rules", "detection_hints"],
	"additionalProperties": false,
	"properties": {
		"module": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
		"display_name": {"type": "string", "minLength": 1},
		"frappe_module_key": {"type": "string", "minLength": 1},
		"backstory": {"type": "string", "minLength": 50},
		"conventions": {
			"type": "object",
			"additionalProperties": false,
			"properties": {
				"permissions_add_roles": {
					"type": "array",
					"items": {
						"type": "object",
						"required": ["role"],
						"properties": {
							"role": {"type": "string", "minLength": 1},
							"read": {"type": "integer", "enum": [0, 1]},
							"write": {"type": "integer", "enum": [0, 1]},
							"create": {"type": "integer", "enum": [0, 1]},
							"delete": {"type": "integer", "enum": [0, 1]}
						}
					}
				},
				"naming_patterns": {"type": "array", "items": {"type": "string"}},
				"typical_linked_doctypes": {"type": "array", "items": {"type": "string"}},
				"required_hooks_for_submittable": {"type": "array", "items": {"type": "string"}},
				"gotchas": {"type": "array", "items": {"type": "string"}}
			}
		},
		"validation_rules": {
			"type": "array",
			"items": {
				"type": "object",
				"required": ["id", "severity", "when", "message"],
				"additionalProperties": false,
				"properties": {
					"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
					"severity": {"enum": ["advisory", "warning", "blocker"]},
					"when": {"type": "object"},
					"message": {"type": "string", "minLength": 1},
					"fix": {"type": "string"}
				}
			}
		},
		"detection_hints": {
			"type": "object",
			"required": ["target_doctype_matches", "keyword_hints"],
			"additionalProperties": false,
			"properties": {
				"target_doctype_matches": {"type": "array", "items": {"type": "string"}},
				"keyword_hints": {"type": "array", "items": {"type": "string"}}
			}
		}
	}
}
```

- [ ] **Step 2: Write the validation test**

Create `tests/test_module_registry_meta_schema.py`:

```python
import json
from pathlib import Path

import jsonschema
import pytest

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "alfred" / "registry" / "modules"


@pytest.fixture(scope="module")
def meta_schema():
	return json.loads((SCHEMA_DIR / "_meta_schema.json").read_text())


def test_module_meta_schema_is_valid_draft_07(meta_schema):
	jsonschema.Draft7Validator.check_schema(meta_schema)


@pytest.mark.parametrize(
	"module_path",
	[p for p in SCHEMA_DIR.glob("*.json") if p.name != "_meta_schema.json"],
	ids=lambda p: p.name,
)
def test_module_kb_validates_against_meta_schema(meta_schema, module_path):
	data = json.loads(module_path.read_text())
	jsonschema.validate(data, meta_schema)
```

- [ ] **Step 3: Run the test**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_registry_meta_schema.py -v`

Expected: `test_module_meta_schema_is_valid_draft_07` passes. Parametrized test collects zero cases (no KB files yet).

- [ ] **Step 4: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/registry/modules/_meta_schema.json tests/test_module_registry_meta_schema.py
git commit -m "feat(module-registry): add meta-schema for module KB files"
```

---

## Task 2: Accounts module KB

**Files:**
- Create: `alfred/registry/modules/accounts.json`

Parametrized test from Task 1 validates it automatically.

- [ ] **Step 1: Create the KB file**

Create `alfred/registry/modules/accounts.json`:

```json
{
	"module": "accounts",
	"display_name": "Accounts",
	"frappe_module_key": "Accounts",
	"backstory": "You are the Alfred ERPNext Accounts domain authority. You know GL posting on_submit of submittable documents, Cost Center / Party Type / Account Head conventions, multi-currency handling and exchange rate sources, Accounts Manager vs Accounts User role separation, period-lock and fiscal-year and posting-date discipline. The anti-patterns you catch include bypassing GL, skipping party validation, and hardcoding currency without an exchange_rate field.",
	"conventions": {
		"permissions_add_roles": [
			{"role": "Accounts Manager", "read": 1, "write": 1, "create": 1, "delete": 1},
			{"role": "Accounts User", "read": 1, "write": 1, "create": 1, "delete": 0}
		],
		"naming_patterns": ["format:ACC-.YYYY.-.####"],
		"typical_linked_doctypes": ["Customer", "Supplier", "Item", "Cost Center", "Account"],
		"required_hooks_for_submittable": ["on_submit_gl_posting"],
		"gotchas": [
			"All submittable DocTypes should post to General Ledger via on_submit hook",
			"Multi-currency fields require explicit exchange_rate and validation",
			"Posting date controls GL entry sequencing - never set it from the client",
			"Fiscal year must be derived, not user-provided"
		]
	},
	"validation_rules": [
		{
			"id": "accounts_submittable_needs_gl",
			"severity": "warning",
			"when": {"doctype": "DocType", "data.is_submittable": 1},
			"message": "Submittable Accounts DocTypes conventionally post GL entries on submit. No on_submit hook detected in the changeset.",
			"fix": "Add a Server Script with doctype_event='on_submit' and reference_doctype=<this DocType> that inserts a GL Entry."
		},
		{
			"id": "accounts_needs_accounts_manager_perm",
			"severity": "advisory",
			"when": {"doctype": "DocType"},
			"message": "Accounts DocTypes typically include an 'Accounts Manager' permission row.",
			"fix": "Add {role: 'Accounts Manager', read: 1, write: 1, create: 1, delete: 1} to permissions."
		}
	],
	"detection_hints": {
		"target_doctype_matches": [
			"Sales Invoice", "Purchase Invoice", "Journal Entry", "Payment Entry",
			"GL Entry", "Cost Center", "Account", "Fiscal Year", "Currency Exchange"
		],
		"keyword_hints": [
			"account", "accounting", "ledger", "gl ", "invoice", "journal",
			"payment entry", "cost center", "fiscal", "currency exchange"
		]
	}
}
```

- [ ] **Step 2: Run meta-schema validation**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_registry_meta_schema.py -v`

Expected: both `test_module_meta_schema_is_valid_draft_07` and `test_module_kb_validates_against_meta_schema[accounts.json]` pass.

- [ ] **Step 3: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/registry/modules/accounts.json
git commit -m "feat(module-registry): add pilot Accounts module KB"
```

---

## Task 3: ModuleRegistry loader

**Files:**
- Create: `alfred/registry/module_loader.py`
- Create: `tests/test_module_registry_loader.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_module_registry_loader.py`:

```python
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
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_registry_loader.py -v`

Expected: `ModuleNotFoundError: No module named 'alfred.registry.module_loader'`.

- [ ] **Step 3: Implement the loader**

Create `alfred/registry/module_loader.py`:

```python
"""Load module KB JSON files and cache them in memory.

Mirrors alfred/registry/loader.py (IntentRegistry). Module KBs declare
per-ERPNext-module conventions, validation rules, and detection hints
that module specialists use to reason about domain correctness.

Spec: docs/specs/2026-04-22-module-specialists.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

SCHEMA_DIR = Path(__file__).parent / "modules"


class UnknownModuleError(KeyError):
	"""Raised when a module key is not in the registry."""


class ModuleRegistry:
	_instance: ClassVar["ModuleRegistry | None"] = None

	def __init__(self, kbs: dict[str, dict]):
		self._by_module = kbs
		self._by_target_doctype: dict[str, dict] = {}
		for kb in kbs.values():
			for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
				self._by_target_doctype[dt] = kb

	@classmethod
	def load(cls) -> "ModuleRegistry":
		if cls._instance is not None:
			return cls._instance
		kbs: dict[str, dict] = {}
		for path in SCHEMA_DIR.glob("*.json"):
			if path.name.startswith("_"):
				continue
			data = json.loads(path.read_text())
			kbs[data["module"]] = data
		cls._instance = cls(kbs)
		return cls._instance

	def modules(self) -> list[str]:
		return sorted(self._by_module.keys())

	def get(self, module: str) -> dict:
		if module not in self._by_module:
			raise UnknownModuleError(module)
		return self._by_module[module]

	def for_doctype(self, doctype: str | None) -> dict | None:
		if not doctype:
			return None
		return self._by_target_doctype.get(doctype)

	def detect(
		self, *, prompt: str, target_doctype: str | None,
	) -> tuple[str | None, str | None]:
		"""Heuristic module detection. Returns (module_key, confidence) or (None, None).

		Confidence ladder:
		- "high" when target_doctype matches a declared hint exactly.
		- "medium" when a keyword hint appears in the lowercased prompt.
		- None when neither path matches.
		"""
		if target_doctype:
			kb = self._by_target_doctype.get(target_doctype)
			if kb is not None:
				return kb["module"], "high"

		low = (prompt or "").lower()
		for kb in self._by_module.values():
			for kw in kb.get("detection_hints", {}).get("keyword_hints", []):
				if kw.lower() in low:
					return kb["module"], "medium"

		return None, None
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_registry_loader.py -v`

Expected: all 9 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/registry/module_loader.py tests/test_module_registry_loader.py
git commit -m "feat(module-registry): add cached ModuleRegistry loader with detection"
```

---

## Task 4: ValidationNote model

**Files:**
- Modify: `alfred/models/agent_outputs.py` (add `ValidationNote` class)
- Create: `tests/test_validation_note.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_validation_note.py`:

```python
import pytest
from pydantic import ValidationError

from alfred.models.agent_outputs import ValidationNote


def test_minimal_fields_required():
	note = ValidationNote(severity="warning", source="module_rule:x", issue="y")
	assert note.severity == "warning"
	assert note.source == "module_rule:x"
	assert note.issue == "y"
	assert note.field is None
	assert note.fix is None
	assert note.changeset_index is None


def test_severity_must_be_known():
	with pytest.raises(ValidationError):
		ValidationNote(severity="bogus", source="x", issue="y")


def test_all_fields_accepted():
	note = ValidationNote(
		severity="blocker",
		source="module_specialist:accounts",
		field="permissions",
		issue="missing role",
		fix="add Accounts Manager",
		changeset_index=0,
	)
	assert note.fix == "add Accounts Manager"
	assert note.changeset_index == 0


def test_serialization_round_trip():
	note = ValidationNote(severity="advisory", source="s", issue="i")
	dumped = note.model_dump()
	restored = ValidationNote.model_validate(dumped)
	assert restored.severity == "advisory"
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_validation_note.py -v`

Expected: `ImportError: cannot import name 'ValidationNote'`.

- [ ] **Step 3: Add ValidationNote to agent_outputs.py**

Open `alfred/models/agent_outputs.py`. Locate the `ChangesetItem` class (it was extended in V1 Task 5). Immediately below `ChangesetItem` and its sibling `Changeset`, add:

```python
class ValidationNote(BaseModel):
	"""Structured note emitted by module specialist's validation pass.

	Shape mirrors the existing dry_run_issues list so the client preview
	panel can render module notes alongside validator notes uniformly.
	Distinguished by ``source`` (e.g. ``"module_specialist:accounts"`` vs.
	``"module_rule:accounts_submittable_needs_gl"``).

	Spec: docs/specs/2026-04-22-module-specialists.md.
	"""

	severity: Literal["advisory", "warning", "blocker"]
	source: str
	issue: str
	field: Optional[str] = None
	fix: Optional[str] = None
	changeset_index: Optional[int] = None
```

(The `Literal` and `Optional` imports already exist at the top of the file from V1 Task 5. If not, add `from typing import Literal, Optional` near the existing imports.)

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_validation_note.py -v`

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/models/agent_outputs.py tests/test_validation_note.py
git commit -m "feat(models): add ValidationNote for module specialist output"
```

---

## Task 5: ModuleDecision + detect_module in orchestrator

**Files:**
- Modify: `alfred/orchestrator.py` (add `ModuleDecision` dataclass + `detect_module` function)
- Create: `tests/test_detect_module.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_detect_module.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest

from alfred.orchestrator import ModuleDecision, detect_module


@pytest.mark.asyncio
async def test_heuristic_matches_via_target_doctype():
	decision = await detect_module(
		prompt="Customize Sales Invoice adding a field",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert decision.module == "accounts"
	assert decision.source == "heuristic"
	assert decision.confidence == "high"


@pytest.mark.asyncio
async def test_heuristic_matches_via_keyword():
	decision = await detect_module(
		prompt="create a journal entry customization",
		target_doctype=None,
		site_config={},
	)
	assert decision.module == "accounts"
	assert decision.source == "heuristic"
	assert decision.confidence == "medium"


@pytest.mark.asyncio
async def test_heuristic_miss_calls_llm():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="accounts"),
	) as llm:
		decision = await detect_module(
			prompt="I need something structured for fiscal period reconciliation",
			target_doctype=None,
			site_config={"llm_tier": "triage"},
		)
		llm.assert_awaited_once()
		assert decision.module == "accounts"
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_llm_returns_unknown():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="unknown"),
	):
		decision = await detect_module(
			prompt="generic prompt", target_doctype=None, site_config={},
		)
		assert decision.module is None
		assert decision.source == "classifier"


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_unknown():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		decision = await detect_module(
			prompt="generic prompt", target_doctype=None, site_config={},
		)
		assert decision.module is None
		assert decision.source == "fallback"


def test_decision_to_dict():
	d = ModuleDecision(module="accounts", reason="r", confidence="high", source="heuristic")
	assert d.to_dict() == {
		"module": "accounts",
		"reason": "r",
		"confidence": "high",
		"source": "heuristic",
	}
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_detect_module.py -v`

Expected: `ImportError: cannot import name 'ModuleDecision'`.

- [ ] **Step 3: Append to orchestrator.py**

Open `alfred/orchestrator.py` and append to the end of the file (after the V1 intent classification block added at the end):

```python
# ── Module detection (Dev mode) ─────────────────────────────────
# Runs after classify_intent for dev-mode prompts to pick a module
# specialist. Heuristic first (ModuleRegistry.detect), LLM fallback
# only when heuristic returns None. Spec:
# docs/specs/2026-04-22-module-specialists.md

from alfred.registry.module_loader import ModuleRegistry as _ModuleRegistry


@dataclass
class ModuleDecision:
	"""Result of per-module Builder classification (dev mode only).

	Mirrors IntentDecision. ``module`` is a registered module key or None
	(None means "no module specialist should be invoked" - identical to
	the flag-off path). ``source`` is one of: "heuristic", "classifier",
	"fallback".
	"""

	module: str | None
	reason: str
	confidence: str  # "high" | "medium" | "low"
	source: str

	def to_dict(self) -> dict:
		return {
			"module": self.module,
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


async def _classify_module_llm(prompt: str, site_config: dict) -> str:
	"""Small LLM call that returns a registered module key or "unknown".

	Kept module-level so tests can patch it without standing up the rest
	of the orchestrator.
	"""
	from alfred.llm_client import ollama_chat

	modules = _ModuleRegistry.load().modules()
	if not modules:
		return "unknown"

	system = (
		"You classify the user's Frappe customization request into ONE ERPNext module. "
		f"Valid modules: {', '.join(modules)}, unknown. "
		"Reply with ONLY the module key, no prose, no punctuation."
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
	return tag if tag in (*modules, "unknown") else "unknown"


async def detect_module(
	*,
	prompt: str,
	target_doctype: str | None,
	site_config: dict,
) -> ModuleDecision:
	registry = _ModuleRegistry.load()
	module_key, confidence = registry.detect(prompt=prompt, target_doctype=target_doctype)
	if module_key is not None:
		return ModuleDecision(
			module=module_key,
			reason=f"matched heuristic ({confidence}) for {module_key}",
			confidence=confidence,
			source="heuristic",
		)

	try:
		tag = await _classify_module_llm(prompt, site_config)
		if tag == "unknown":
			return ModuleDecision(
				module=None,
				reason="LLM classifier returned unknown",
				confidence="low",
				source="classifier",
			)
		return ModuleDecision(
			module=tag,
			reason=f"LLM classifier returned {tag}",
			confidence="medium",
			source="classifier",
		)
	except Exception as e:
		logger.warning("Module classifier failed: %s", e)
		return ModuleDecision(
			module=None,
			reason=f"classifier error: {e}",
			confidence="low",
			source="fallback",
		)
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_detect_module.py -v`

Expected: all 6 tests pass. Also confirm `tests/test_orchestrator.py` still passes: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_orchestrator.py -q`

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/orchestrator.py tests/test_detect_module.py
git commit -m "feat(orchestrator): add ModuleDecision and detect_module"
```

---

## Task 6: Module specialist deterministic rule runner

**Files:**
- Create: `alfred/agents/specialists/__init__.py`
- Create: `alfred/agents/specialists/module_specialist.py` (rule-runner half only; LLM half comes in Task 7)
- Create: `tests/test_module_specialist_rules.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_module_specialist_rules.py`:

```python
from alfred.agents.specialists.module_specialist import run_rule_validation
from alfred.models.agent_outputs import ValidationNote


def test_submittable_doctype_without_gl_hook_triggers_warning():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Accounts Voucher", "is_submittable": 1},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert any(n.source == "module_rule:accounts_submittable_needs_gl" for n in notes)
	submittable_note = next(
		n for n in notes if n.source == "module_rule:accounts_submittable_needs_gl"
	)
	assert submittable_note.severity == "warning"
	assert submittable_note.changeset_index == 0


def test_non_submittable_doctype_does_not_trigger_gl_warning():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Ledger Note", "is_submittable": 0},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert not any(n.source == "module_rule:accounts_submittable_needs_gl" for n in notes)


def test_doctype_without_accounts_manager_triggers_advisory():
	changes = [
		{
			"op": "create",
			"doctype": "DocType",
			"data": {"name": "Accounts Voucher"},
		},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert any(n.source == "module_rule:accounts_needs_accounts_manager_perm" for n in notes)
	adv = next(
		n for n in notes if n.source == "module_rule:accounts_needs_accounts_manager_perm"
	)
	assert adv.severity == "advisory"


def test_non_doctype_item_ignored_by_doctype_rule():
	changes = [
		{"op": "create", "doctype": "Custom Field", "data": {"fieldname": "x"}},
	]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert notes == []


def test_unknown_module_returns_empty():
	changes = [{"op": "create", "doctype": "DocType", "data": {}}]
	notes = run_rule_validation(module="not_a_real_module", changes=changes)
	assert notes == []


def test_empty_changes_returns_empty():
	notes = run_rule_validation(module="accounts", changes=[])
	assert notes == []


def test_rule_notes_are_validation_note_instances():
	changes = [{"op": "create", "doctype": "DocType", "data": {"is_submittable": 1}}]
	notes = run_rule_validation(module="accounts", changes=changes)
	assert all(isinstance(n, ValidationNote) for n in notes)
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_specialist_rules.py -v`

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Create package marker**

Create `alfred/agents/specialists/__init__.py` with a single blank line.

- [ ] **Step 4: Implement the rule runner**

Create `alfred/agents/specialists/module_specialist.py`:

```python
"""Module specialist - invoked twice per Dev-mode build.

- provide_context(module, intent, target_doctype, site_config)
      LLM call at the start of build: returns a prompt snippet summarising
      module-specific conventions relevant to the intent.

- validate_output(module, intent, changes, site_config)
      LLM call at the end of build: returns a list of ValidationNotes where
      module conventions were dropped, contradicted, or misapplied.

This file ships in two phases:
  Task 6: deterministic rule runner (run_rule_validation). No LLM.
  Task 7: LLM-backed provide_context + validate_output that wrap the rule
          runner and merge its notes with LLM-discovered notes.

Spec: docs/specs/2026-04-22-module-specialists.md.
"""

from __future__ import annotations

import logging

from alfred.models.agent_outputs import ValidationNote
from alfred.registry.module_loader import ModuleRegistry, UnknownModuleError

logger = logging.getLogger("alfred.agents.specialists.module")


def _rule_applies(when: dict, item: dict) -> bool:
	"""Check the rule's ``when`` clause against a single changeset item.

	The ``when`` clause is a dict of dotted-path keys to expected values.
	Examples:
	  {"doctype": "DocType"}                         -> item["doctype"] == "DocType"
	  {"doctype": "DocType", "data.is_submittable": 1} -> both must hold.
	"""
	for key, expected in when.items():
		actual = item
		for part in key.split("."):
			if not isinstance(actual, dict):
				return False
			actual = actual.get(part)
		if actual != expected:
			return False
	return True


def run_rule_validation(
	*, module: str, changes: list[dict],
) -> list[ValidationNote]:
	"""Apply a module's declared validation_rules to each change item.

	Deterministic; no LLM. Returns a list of ValidationNotes. Unknown
	module or empty changes -> empty list. Individual rule failures are
	caught and logged so a malformed rule doesn't block the pipeline.
	"""
	if not changes:
		return []

	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return []

	notes: list[ValidationNote] = []
	for idx, item in enumerate(changes):
		for rule in kb.get("validation_rules", []):
			try:
				if _rule_applies(rule["when"], item):
					notes.append(ValidationNote(
						severity=rule["severity"],
						source=f"module_rule:{rule['id']}",
						issue=rule["message"],
						fix=rule.get("fix"),
						changeset_index=idx,
					))
			except Exception as e:
				logger.warning(
					"Module rule %s failed while evaluating change %d: %s",
					rule.get("id", "<unknown>"), idx, e,
				)
	return notes
```

- [ ] **Step 5: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_specialist_rules.py -v`

Expected: all 7 tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/specialists/ tests/test_module_specialist_rules.py
git commit -m "feat(specialists): add deterministic rule runner for module validation"
```

---

## Task 7: Module specialist LLM wrappers (provide_context + validate_output)

**Files:**
- Modify: `alfred/agents/specialists/module_specialist.py` (add async `provide_context` and `validate_output` functions)
- Create: `tests/test_module_specialist_llm.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_module_specialist_llm.py`:

```python
import json
from unittest.mock import AsyncMock, patch

import pytest

from alfred.agents.specialists.module_specialist import (
	provide_context,
	validate_output,
)


@pytest.mark.asyncio
async def test_provide_context_unknown_module_returns_empty():
	out = await provide_context(
		module="not_a_real_module",
		intent="create_doctype",
		target_doctype=None,
		site_config={},
	)
	assert out == ""


@pytest.mark.asyncio
async def test_provide_context_calls_llm_with_module_backstory():
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="Accounts context snippet"),
	) as llm:
		out = await provide_context(
			module="accounts",
			intent="create_doctype",
			target_doctype="Sales Invoice",
			site_config={},
		)
		llm.assert_awaited_once()
		call_messages = llm.await_args.kwargs["messages"]
		# System message carries the KB backstory
		system = call_messages[0]["content"]
		assert "Accounts domain authority" in system
		# User message names the intent and target
		user = call_messages[1]["content"]
		assert "create_doctype" in user
		assert "Sales Invoice" in user
		assert out == "Accounts context snippet"


@pytest.mark.asyncio
async def test_provide_context_llm_failure_returns_empty():
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(side_effect=RuntimeError("boom")),
	):
		out = await provide_context(
			module="accounts",
			intent="create_doctype",
			target_doctype=None,
			site_config={},
		)
		assert out == ""


@pytest.mark.asyncio
async def test_validate_output_unknown_module_returns_empty():
	notes = await validate_output(
		module="not_a_real_module",
		intent="create_doctype",
		changes=[{"op": "create", "doctype": "DocType", "data": {}}],
		site_config={},
	)
	assert notes == []


@pytest.mark.asyncio
async def test_validate_output_empty_changes_returns_empty():
	notes = await validate_output(
		module="accounts",
		intent="create_doctype",
		changes=[],
		site_config={},
	)
	assert notes == []


@pytest.mark.asyncio
async def test_validate_output_merges_rule_notes_and_llm_notes():
	llm_reply = json.dumps([
		{
			"severity": "advisory",
			"issue": "LLM-found advisory",
			"field": "something",
			"fix": "do X",
		},
	])
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value=llm_reply),
	):
		notes = await validate_output(
			module="accounts",
			intent="create_doctype",
			changes=[{
				"op": "create", "doctype": "DocType",
				"data": {"name": "Voucher", "is_submittable": 1},
			}],
			site_config={},
		)
	sources = {n.source for n in notes}
	# Rule-runner caught submittable + missing Accounts Manager permission (2 notes)
	assert "module_rule:accounts_submittable_needs_gl" in sources
	assert "module_rule:accounts_needs_accounts_manager_perm" in sources
	# LLM note also surfaced
	assert any(s.startswith("module_specialist:") for s in sources)


@pytest.mark.asyncio
async def test_validate_output_llm_malformed_json_falls_back_to_rules_only():
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="not valid json at all"),
	):
		notes = await validate_output(
			module="accounts",
			intent="create_doctype",
			changes=[{
				"op": "create", "doctype": "DocType",
				"data": {"name": "Voucher", "is_submittable": 1},
			}],
			site_config={},
		)
	sources = {n.source for n in notes}
	# Rules still applied
	assert "module_rule:accounts_submittable_needs_gl" in sources
	# No LLM note (since JSON parse failed)
	assert not any(s.startswith("module_specialist:") for s in sources)
```

- [ ] **Step 2: Run — expect import error**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_specialist_llm.py -v`

Expected: `ImportError: cannot import name 'provide_context'`.

- [ ] **Step 3: Add the LLM wrappers to module_specialist.py**

Open `alfred/agents/specialists/module_specialist.py` and append (keep the existing `run_rule_validation` function intact):

```python
import json
import re

from alfred.llm_client import ollama_chat as _ollama_chat  # re-exported for test patching

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def _strip_code_fences(text: str) -> str:
	if not text:
		return text
	cleaned = text.strip()
	if cleaned.startswith("```"):
		lines = cleaned.splitlines()
		if lines and lines[0].startswith("```"):
			lines = lines[1:]
		if lines and lines[-1].startswith("```"):
			lines = lines[:-1]
		cleaned = "\n".join(lines).strip()
	return cleaned


def _parse_llm_note_list(raw: str) -> list[dict] | None:
	"""Parse the LLM's validation response as a JSON array of note dicts.

	Mirrors handlers/plan.py's _parse_plan_doc_json robustness. Returns
	None on unrecoverable parse failure so callers can fall back to
	rule-only notes.
	"""
	if not raw:
		return None
	cleaned = _strip_code_fences(raw)
	try:
		parsed = json.loads(cleaned)
		if isinstance(parsed, list):
			return parsed
	except Exception:
		pass

	decoder = json.JSONDecoder()
	for idx, ch in enumerate(cleaned):
		if ch != "[":
			continue
		try:
			parsed, _ = decoder.raw_decode(cleaned[idx:])
			if isinstance(parsed, list):
				return parsed
		except Exception:
			continue
	return None


async def provide_context(
	*,
	module: str,
	intent: str,
	target_doctype: str | None,
	site_config: dict,
) -> str:
	"""Context pre-pass. Returns a prompt snippet for the intent specialist."""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return ""

	user_parts = [f"Intent: {intent}"]
	if target_doctype:
		user_parts.append(f"Target DocType: {target_doctype}")
	user_parts.append(
		"Summarise the subset of your module knowledge relevant to this "
		"intent + target. Output should be 3-6 sentences of concrete "
		"conventions, role names, and gotchas. No prose introduction, no "
		"JSON, no markdown headers."
	)

	try:
		reply = await _ollama_chat(
			messages=[
				{"role": "system", "content": kb["backstory"]},
				{"role": "user", "content": "\n".join(user_parts)},
			],
			site_config=site_config,
			tier=site_config.get("llm_tier", "triage"),
			max_tokens=400,
			temperature=0.2,
		)
		return (reply or "").strip()
	except Exception as e:
		logger.warning("Module specialist provide_context failed (%s): %s", module, e)
		return ""


async def validate_output(
	*,
	module: str,
	intent: str,
	changes: list[dict],
	site_config: dict,
) -> list[ValidationNote]:
	"""Validation post-pass. Merges deterministic rule notes with LLM notes."""
	if not changes:
		return []

	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return []

	# Rule-runner half (deterministic, always runs)
	notes = run_rule_validation(module=module, changes=changes)

	# LLM half
	prompt_body = (
		f"Intent: {intent}\n"
		f"Changeset (JSON):\n{json.dumps(changes, indent=2)[:4000]}\n\n"
		"Review the changeset against your module's conventions. Emit a "
		"JSON array of notes where domain conventions have been dropped, "
		"contradicted, or misapplied. Each note has keys: severity "
		"(advisory|warning|blocker), issue (short string), field (optional "
		"dotted path), fix (optional string). Output ONLY the JSON array. "
		"If nothing is wrong, output: []"
	)

	try:
		raw = await _ollama_chat(
			messages=[
				{"role": "system", "content": kb["backstory"]},
				{"role": "user", "content": prompt_body},
			],
			site_config=site_config,
			tier=site_config.get("llm_tier", "triage"),
			max_tokens=600,
			temperature=0.1,
		)
	except Exception as e:
		logger.warning("Module specialist validate_output LLM failed (%s): %s", module, e)
		return notes

	parsed = _parse_llm_note_list(raw or "")
	if parsed is None:
		logger.warning(
			"Module specialist validate_output returned unparseable JSON (first 300): %r",
			(raw or "")[:300],
		)
		return notes

	for entry in parsed:
		if not isinstance(entry, dict):
			continue
		try:
			notes.append(ValidationNote(
				severity=entry.get("severity", "advisory"),
				source=f"module_specialist:{module}",
				issue=entry.get("issue", ""),
				field=entry.get("field"),
				fix=entry.get("fix"),
			))
		except Exception as e:
			logger.debug("Dropping malformed LLM note %r: %s", entry, e)

	return notes
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_module_specialist_llm.py tests/test_module_specialist_rules.py -v`

Expected: all 14 tests pass (7 rule + 7 LLM).

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/specialists/module_specialist.py tests/test_module_specialist_llm.py
git commit -m "feat(specialists): add LLM-backed provide_context and validate_output"
```

---

## Task 8: Extend DocType Builder prompt enhancer with module context

**Files:**
- Modify: `alfred/agents/builders/doctype_builder.py` (extend `enhance_generate_changeset_description` signature)
- Modify: `tests/test_doctype_builder.py` (add module_context tests)

- [ ] **Step 1: Add the failing tests**

Append to `tests/test_doctype_builder.py`:

```python
def test_enhance_with_module_context_appends_both_sections():
	base = "BASE"
	out = enhance_generate_changeset_description(base, module_context="accounts convention snippet")
	assert "BASE" in out
	assert "field_defaults_meta" in out  # intent checklist still applied
	assert "accounts convention snippet" in out  # module context appended


def test_enhance_with_empty_module_context_matches_v1_behaviour():
	base = "BASE"
	out = enhance_generate_changeset_description(base, module_context="")
	assert "BASE" in out
	assert "field_defaults_meta" in out
	# No module wrapper marker when no context
	assert "MODULE CONTEXT" not in out


def test_enhance_with_module_context_is_idempotent():
	base = "BASE"
	once = enhance_generate_changeset_description(base, module_context="snip")
	twice = enhance_generate_changeset_description(once, module_context="snip")
	assert once == twice


def test_enhance_appends_module_context_to_already_enhanced_base():
	# V1 applied checklist first; V2 then wants to add module context
	first = enhance_generate_changeset_description("BASE")
	assert "MODULE CONTEXT" not in first
	second = enhance_generate_changeset_description(first, module_context="snip")
	assert "snip" in second
	assert "MODULE CONTEXT" in second
```

- [ ] **Step 2: Run — expect fail**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_doctype_builder.py -v`

Expected: the four new tests fail (signature doesn't accept `module_context`, or module context not appended).

- [ ] **Step 3: Update doctype_builder.py**

Open `alfred/agents/builders/doctype_builder.py`. Below the existing `_CHECKLIST_MARKER` constant, add:

```python
_MODULE_CONTEXT_MARKER = "MODULE CONTEXT"


def _wrap_module_context(snippet: str) -> str:
	return (
		f"{_MODULE_CONTEXT_MARKER} (target-module conventions — respect these "
		"alongside the shape-defining fields above):\n"
		f"{snippet}"
	)
```

Replace the existing `enhance_generate_changeset_description` function body with:

```python
def enhance_generate_changeset_description(base: str, module_context: str = "") -> str:
	"""Return the base generate_changeset description with intent checklist and optional module context appended.

	Idempotent per section: the intent checklist is appended once (guarded
	by _CHECKLIST_MARKER), and the module context is appended once
	(guarded by _MODULE_CONTEXT_MARKER). Double-enhance is a no-op.
	"""
	out = base
	if _CHECKLIST_MARKER not in out:
		schema = IntentRegistry.load().get("create_doctype")
		out = out + "\n\n" + render_registry_checklist(schema)
	if module_context and _MODULE_CONTEXT_MARKER not in out:
		out = out + "\n\n" + _wrap_module_context(module_context)
	return out
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_doctype_builder.py -v`

Expected: all 11 tests pass (7 existing + 4 new).

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/builders/doctype_builder.py tests/test_doctype_builder.py
git commit -m "feat(builders): accept optional module_context in DocType prompt enhancer"
```

---

## Task 9: Thread module_context through crew dispatch

**Files:**
- Modify: `alfred/agents/crew.py` (extend `build_alfred_crew` and `_enhance_task_description`)
- Modify: `tests/test_crew_specialist_dispatch.py` (add module_context assertions)

- [ ] **Step 1: Add failing test**

Append to `tests/test_crew_specialist_dispatch.py`:

```python
def test_enhance_task_description_injects_module_context_when_provided():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text",
			module_context="accounts snippet",
		)
		assert "base text" in out
		assert "field_defaults_meta" in out
		assert "accounts snippet" in out


def test_enhance_task_description_ignores_module_context_for_other_tasks():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		out = _enhance_task_description(
			"gather_requirements", "create_doctype", "base text",
			module_context="accounts snippet",
		)
		assert out == "base text"


def test_enhance_task_description_empty_module_context_equals_v1_path():
	with patch.dict(os.environ, {"ALFRED_PER_INTENT_BUILDERS": "1"}):
		out = _enhance_task_description(
			"generate_changeset", "create_doctype", "base text",
			module_context="",
		)
		assert "field_defaults_meta" in out
		assert "MODULE CONTEXT" not in out
```

- [ ] **Step 2: Run — expect fail**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_crew_specialist_dispatch.py -v`

Expected: new tests fail with unexpected-keyword or missing-content assertions.

- [ ] **Step 3: Extend `_enhance_task_description`**

In `alfred/agents/crew.py`, locate `_enhance_task_description` (added at the end of the file in V1 Task 8). Replace the function with:

```python
def _enhance_task_description(
	task_name: str,
	intent: str | None,
	base_description: str,
	module_context: str = "",
) -> str:
	"""Return a possibly-enhanced description for a given task + intent + module.

	Only ``generate_changeset`` is enhanced. All other task descriptions
	pass through unchanged. When the flag is off or the intent has no
	specialist, the base description is returned unchanged regardless of
	module_context.
	"""
	if not _per_intent_builders_enabled():
		return base_description
	if task_name != "generate_changeset":
		return base_description
	if not intent or intent == "unknown":
		return base_description

	if intent == "create_doctype":
		from alfred.agents.builders.doctype_builder import enhance_generate_changeset_description
		return enhance_generate_changeset_description(
			base_description, module_context=module_context,
		)

	return base_description
```

- [ ] **Step 4: Extend `build_alfred_crew` to accept and thread module_context**

Locate the `build_alfred_crew` signature (V1 already added `intent`). Add `module_context`:

```python
def build_alfred_crew(
	user_prompt: str,
	user_context: dict | None = None,
	site_config: dict | None = None,
	previous_state: CrewState | None = None,
	custom_tools: dict | None = None,
	intent: str | None = None,
	module_context: str = "",
) -> tuple[Crew, CrewState]:
```

Locate the existing call site where `_enhance_task_description` is invoked (inside the task-definition loop). Pass `module_context` through:

```python
		base_description = _enhance_task_description(
			task_name, intent, desc_template["description"],
			module_context=module_context,
		)
```

- [ ] **Step 5: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_crew_specialist_dispatch.py -v`

Expected: all 12 tests pass (9 existing + 3 new).

Regression: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_doctype_builder.py tests/test_crew_specialist_dispatch.py -q`. Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/agents/crew.py tests/test_crew_specialist_dispatch.py
git commit -m "feat(crew): thread module_context through generate_changeset enhancement"
```

---

## Task 10: Extend backfill with module-aware defaults

**Files:**
- Modify: `alfred/handlers/post_build/backfill_defaults.py` (add optional `module` kwarg to `backfill_defaults_raw`)
- Create: `tests/test_backfill_module_defaults.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_backfill_module_defaults.py`:

```python
from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


def _doctype_change(data):
	return {"op": "create", "doctype": "DocType", "data": data}


def test_module_none_behaves_like_v1():
	changes = [_doctype_change({"name": "Book", "module": "Custom"})]
	out = backfill_defaults_raw(changes)  # no module kwarg
	# V1 behaviour: intent default System Manager row only
	perms = out[0]["data"]["permissions"]
	roles = {p["role"] for p in perms}
	assert "System Manager" in roles
	assert "Accounts Manager" not in roles


def test_module_accounts_adds_accounts_roles():
	changes = [_doctype_change({"name": "Voucher", "module": "Custom"})]
	out = backfill_defaults_raw(changes, module="accounts")
	perms = out[0]["data"]["permissions"]
	roles = {p["role"] for p in perms}
	assert "System Manager" in roles  # intent default
	assert "Accounts Manager" in roles  # module default
	assert "Accounts User" in roles  # module default


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
```

- [ ] **Step 2: Run — expect fail**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_backfill_module_defaults.py -v`

Expected: `TypeError: backfill_defaults_raw() got an unexpected keyword argument 'module'` or assertions fail.

- [ ] **Step 3: Extend the backfill module**

Open `alfred/handlers/post_build/backfill_defaults.py`. At the top, add:

```python
from alfred.registry.module_loader import ModuleRegistry, UnknownModuleError
```

Replace the existing `backfill_defaults_raw` function with:

```python
def backfill_defaults_raw(
	changes: list[dict], *, module: str | None = None,
) -> list[dict]:
	"""Raw-dict variant used by the pipeline.

	V1 behaviour (module is None): fills missing intent-registry fields in
	``data`` and appends a ``field_defaults_meta`` annotation.

	V2 behaviour (module set): after V1 pass, layers the module KB's
	conventions on top - adds any missing ``permissions_add_roles`` entries
	and, if the V1 pass defaulted ``autoname``, swaps in the first entry
	from the module's ``naming_patterns`` with a module-aware rationale.
	Unknown module keys fall back to V1 behaviour.
	"""
	registry = IntentRegistry.load()
	out: list[dict] = []
	for change in changes:
		doctype = change.get("doctype")
		schema = registry.for_doctype(doctype) if doctype else None
		if schema is None:
			out.append(change)
			continue
		backfilled = _backfill_raw(change, schema)
		if module:
			backfilled = _apply_module_defaults(backfilled, module)
		out.append(backfilled)
	return out


def _apply_module_defaults(change: dict, module: str) -> dict:
	"""Layer a module KB's conventions on top of an intent-backfilled change."""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return change

	display_name = kb.get("display_name", module)
	conv = kb.get("conventions", {})
	new = {**change}
	data = copy.deepcopy(new.get("data") or {})
	meta = copy.deepcopy(new.get("field_defaults_meta") or {})

	# 1. Permissions: add any module roles that aren't already present.
	existing_perms = data.get("permissions") or []
	existing_roles = {p.get("role") for p in existing_perms if isinstance(p, dict)}
	appended: list[str] = []
	for row in conv.get("permissions_add_roles", []):
		if row.get("role") and row["role"] not in existing_roles:
			existing_perms.append(copy.deepcopy(row))
			existing_roles.add(row["role"])
			appended.append(row["role"])
	if appended:
		data["permissions"] = existing_perms
		prev = meta.get("permissions", {})
		prev_rationale = prev.get("rationale", "")
		module_reason = f"Added {', '.join(appended)} because target module is {display_name}."
		meta["permissions"] = {
			"source": "default",
			"rationale": (prev_rationale + " " + module_reason).strip() if prev_rationale else module_reason,
		}

	# 2. Autoname swap: only if intent backfill defaulted it.
	auto_meta = meta.get("autoname") or {}
	naming_patterns = conv.get("naming_patterns") or []
	if auto_meta.get("source") == "default" and naming_patterns:
		data["autoname"] = naming_patterns[0]
		meta["autoname"] = {
			"source": "default",
			"rationale": (
				f"Module {display_name} conventionally uses {naming_patterns[0]}."
			),
		}

	new["data"] = data
	new["field_defaults_meta"] = meta
	return new
```

- [ ] **Step 4: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_backfill_module_defaults.py tests/test_backfill_defaults_raw.py tests/test_backfill_defaults.py -v`

Expected: all tests pass (6 new + existing 10).

- [ ] **Step 5: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/handlers/post_build/backfill_defaults.py tests/test_backfill_module_defaults.py
git commit -m "feat(backfill): layer module KB defaults on top of intent defaults"
```

---

## Task 11: Pipeline wiring — classify_module + provide_module_context + post-crew validation

**Files:**
- Modify: `alfred/api/pipeline.py` (add two phases, extend `PipelineContext`, call validator in `_phase_post_crew`)
- Create: `tests/test_pipeline_module_integration.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_pipeline_module_integration.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext


def _build_ctx(prompt: str, mode: str = "dev") -> PipelineContext:
	conn = MagicMock()
	conn.site_config = {}
	ctx = PipelineContext(conn=conn, conversation_id="test-conv", prompt=prompt)
	ctx.mode = mode
	return ctx


def test_phases_includes_classify_module_and_provide_module_context_in_order():
	phases = AgentPipeline.PHASES
	assert "classify_module" in phases
	assert "provide_module_context" in phases
	assert phases.index("classify_intent") < phases.index("classify_module")
	assert phases.index("classify_module") < phases.index("provide_module_context")
	assert phases.index("provide_module_context") < phases.index("build_crew")


@pytest.mark.asyncio
async def test_classify_module_noop_for_non_dev_mode(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice", mode="plan")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_noop_when_v2_flag_off(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.delenv("ALFRED_MODULE_SPECIALISTS", raising=False)
	ctx = _build_ctx("Customize Sales Invoice", mode="dev")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_noop_when_v1_flag_off(monkeypatch):
	monkeypatch.delenv("ALFRED_PER_INTENT_BUILDERS", raising=False)
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice", mode="dev")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module is None


@pytest.mark.asyncio
async def test_classify_module_populates_ctx_on_heuristic_match(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("Customize Sales Invoice with a new field")
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_classify_module()
	assert ctx.module == "accounts"
	assert ctx.module_source == "heuristic"


@pytest.mark.asyncio
async def test_provide_module_context_noop_when_module_is_none(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("prompt")
	ctx.module = None
	pipeline = AgentPipeline(ctx)
	await pipeline._phase_provide_module_context()
	assert ctx.module_context == ""


@pytest.mark.asyncio
async def test_provide_module_context_calls_specialist_when_module_set(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	ctx = _build_ctx("prompt")
	ctx.module = "accounts"
	ctx.intent = "create_doctype"
	pipeline = AgentPipeline(ctx)
	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(return_value="accounts snippet"),
	) as spy:
		await pipeline._phase_provide_module_context()
		spy.assert_awaited_once()
		assert ctx.module_context == "accounts snippet"
```

- [ ] **Step 2: Run — expect fail**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_pipeline_module_integration.py -v`

Expected: `PHASES` does not include `classify_module`; `_phase_classify_module` doesn't exist.

- [ ] **Step 3: Extend `PipelineContext`**

Open `alfred/api/pipeline.py`. Locate the `PipelineContext` dataclass block (V1 added `intent*` fields there). Immediately below the V1 intent block, add:

```python
	# Per-module Builder classification (dev mode only, V2).
	# Written by _phase_classify_module when both V1 (ALFRED_PER_INTENT_BUILDERS)
	# and V2 (ALFRED_MODULE_SPECIALISTS) flags are on. Flows into
	# _phase_provide_module_context (which populates module_context), into
	# _phase_build_crew (module_context threaded to specialist), into
	# _phase_post_crew (module-aware backfill + validation).
	# Spec: docs/specs/2026-04-22-module-specialists.md.
	module: str | None = None
	module_confidence: str | None = None
	module_source: str | None = None
	module_reason: str | None = None
	module_context: str = ""
	module_validation_notes: list[dict] = field(default_factory=list)
```

- [ ] **Step 4: Add phases to the PHASES list**

Locate `PHASES: list[str] = [...]`. V1 added `"classify_intent"`. Add `classify_module` and `provide_module_context` in the right positions:

```python
	PHASES: list[str] = [
		"sanitize",
		"load_state",
		"warmup",
		"plan_check",
		"orchestrate",
		"classify_intent",
		"classify_module",
		"enhance",
		"clarify",
		"inject_kb",
		"resolve_mode",
		"provide_module_context",
		"build_crew",
		"run_crew",
		"post_crew",
	]
```

- [ ] **Step 5: Add `_phase_classify_module` method**

Find `_phase_classify_intent` (V1). Below it (before `_phase_enhance`), add:

```python
	async def _phase_classify_module(self) -> None:
		"""Classify the dev-mode prompt's target module for specialist selection.

		No-op for non-dev modes, when ALFRED_PER_INTENT_BUILDERS is off, or
		when ALFRED_MODULE_SPECIALISTS is off. Stores the ModuleDecision
		fields on ctx.module* for downstream phases to read.

		See docs/specs/2026-04-22-module-specialists.md.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		if _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") != "1":
			return
		if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") != "1":
			return

		from alfred.orchestrator import detect_module

		# Target DocType, if any, is typically encoded in the prompt. Keep
		# extraction simple for V2: the ModuleRegistry.detect() heuristic
		# scans the full prompt for target_doctype_matches verbatim.
		# Future work: share a named-entity extraction step with the
		# intent classifier.
		decision = await detect_module(
			prompt=ctx.prompt,
			target_doctype=None,
			site_config=ctx.conn.site_config or {},
		)
		ctx.module = decision.module
		ctx.module_source = decision.source
		ctx.module_confidence = decision.confidence
		ctx.module_reason = decision.reason

		logger.info(
			"Module decision for conversation=%s: module=%s source=%s confidence=%s reason=%r",
			ctx.conversation_id, decision.module, decision.source,
			decision.confidence, decision.reason,
		)
```

- [ ] **Step 6: Add `_phase_provide_module_context` method**

Below the previous method, add:

```python
	async def _phase_provide_module_context(self) -> None:
		"""Invoke module specialist's context pass; stash snippet on ctx.

		No-op when flags off or when no module was detected.
		"""
		ctx = self.ctx
		if ctx.mode != "dev":
			return
		if _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") != "1":
			return
		if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") != "1":
			return
		if not ctx.module:
			return

		from alfred.agents.specialists.module_specialist import provide_context

		try:
			snippet = await provide_context(
				module=ctx.module,
				intent=ctx.intent or "unknown",
				target_doctype=None,
				site_config=ctx.conn.site_config or {},
			)
		except Exception as e:
			logger.warning(
				"provide_module_context failed for conversation=%s module=%s: %s",
				ctx.conversation_id, ctx.module, e,
			)
			snippet = ""

		ctx.module_context = snippet
```

- [ ] **Step 7: Pass `module_context` to `build_alfred_crew`**

Find the call `ctx.crew, ctx.crew_state = build_alfred_crew(...)` in `_phase_build_crew` (V1 added `intent=ctx.intent`). Add a second V2 kwarg:

```python
			ctx.crew, ctx.crew_state = build_alfred_crew(
				user_prompt=ctx.enhanced_prompt,
				user_context=ctx.user_context,
				site_config=ctx.conn.site_config,
				previous_state=None,
				custom_tools=ctx.custom_tools,
				intent=ctx.intent,
				module_context=ctx.module_context,
			)
```

- [ ] **Step 8: Extend backfill call to pass module**

Locate the V1 backfill call inside `_phase_post_crew`:

```python
if ctx.changes and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1":
	from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw
	try:
		ctx.changes = backfill_defaults_raw(ctx.changes)
	except Exception as e:
		...
```

Replace the `backfill_defaults_raw(ctx.changes)` call with:

```python
		ctx.changes = backfill_defaults_raw(
			ctx.changes,
			module=ctx.module if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1" else None,
		)
```

- [ ] **Step 9: Add module validation after backfill**

Immediately after the backfill try/except block, append:

```python
		# V2: module specialist validation pass. Runs only when both flags
		# on, a module was detected, and we have changes to validate.
		if (
			ctx.changes
			and ctx.module
			and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1"
			and _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1"
		):
			from alfred.agents.specialists.module_specialist import validate_output
			try:
				notes = await validate_output(
					module=ctx.module,
					intent=ctx.intent or "unknown",
					changes=ctx.changes,
					site_config=ctx.conn.site_config or {},
				)
				ctx.module_validation_notes = [n.model_dump() for n in notes]
			except Exception as e:
				logger.warning(
					"validate_output failed for conversation=%s module=%s: %s",
					ctx.conversation_id, ctx.module, e,
				)
				ctx.module_validation_notes = []
```

- [ ] **Step 10: Emit module_validation_notes in the WebSocket payload**

Locate the end of `_phase_post_crew` where `ctx.changes` is sent to the client. Add `module_validation_notes` alongside `changes` in the emission:

```python
		await ctx.conn.send({
			"msg_id": str(uuid.uuid4()),
			"type": "changeset",
			"data": {
				"conversation": ctx.conversation_id,
				"changes": ctx.changes,
				"module_validation_notes": ctx.module_validation_notes,
				# ... other existing fields unchanged
			},
		})
```

The exact enclosing `send` call in today's pipeline already carries a `data` dict with `changes`; locate it near line 1639 in pipeline.py and add the `module_validation_notes` key alongside `changes`. If the existing call is more deeply nested (wrapped in other helpers), add the key wherever `changes` is added to the payload.

- [ ] **Step 11: Run — expect pass**

Run: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && .venv/bin/python -m pytest tests/test_pipeline_module_integration.py tests/test_pipeline_specialist_integration.py -v`

Expected: all new tests pass; V1 pipeline integration tests also still pass.

- [ ] **Step 12: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
git add alfred/api/pipeline.py tests/test_pipeline_module_integration.py
git commit -m "feat(pipeline): add classify_module + provide_module_context phases and module validation post-pass"
```

---

## Task 12: Client preview — render module_validation_notes + module badge

**Files:**
- Modify: `/Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client/alfred_client/public/js/alfred_chat/PreviewPanel.vue`

- [ ] **Step 1: Locate the changeset payload binding**

Open `PreviewPanel.vue`. V1 rendered `field_defaults_meta` on DocType items; V2 needs two new surface areas:

1. A module badge at the top of the changeset preview showing which module was detected.
2. Validation-note banners for each item in `changeset.module_validation_notes`.

V1's `changes` computed (line ~519) reads `props.changeset?.changes`. Confirm the payload shape from Task 11 — the client will now receive `module_validation_notes` as a sibling of `changes`.

- [ ] **Step 2: Add a `moduleValidationNotes` computed**

In the `<script setup>` section, below the existing `dryRunIssues` computed, add:

```javascript
const moduleValidationNotes = computed(() => {
	const raw = props.changeset?.module_validation_notes;
	if (!raw) return [];
	if (Array.isArray(raw)) return raw;
	if (typeof raw === "string") {
		try { return JSON.parse(raw); } catch { return []; }
	}
	return [];
});

const detectedModuleDisplay = computed(() => {
	// The pipeline doesn't send module_display_name yet; show the raw key
	// when present, else empty. Future: plumb display_name through.
	return props.changeset?.detected_module || "";
});
```

- [ ] **Step 3: Render the module badge**

Locate the `<div v-else-if="changeset" class="alfred-preview-content">` block (around line 112 of the current PreviewPanel). Immediately above the PENDING banners, insert:

```vue
				<div v-if="detectedModuleDisplay" class="alfred-module-badge">
					<span class="alfred-module-badge__icon" aria-hidden="true">&#9675;</span>
					<span class="alfred-module-badge__label">
						{{ __("Module context:") }} <strong>{{ detectedModuleDisplay }}</strong>
					</span>
				</div>
```

- [ ] **Step 4: Render module validation notes**

Immediately below the existing `<div v-else-if="previewState === 'PENDING' && dryRunIssues.length" ...>` block (the dry-run-issues warn banner), add a parallel block for module validation:

```vue
				<div
					v-if="previewState === 'PENDING' && moduleValidationNotes.length"
					class="alfred-banner alfred-banner--module-notes"
				>
					<span class="alfred-banner__icon" aria-hidden="true">&#9873;</span>
					<div class="alfred-banner__body">
						<strong>{{ __("{0} module convention note(s)", [moduleValidationNotes.length]) }}</strong>
						<ul class="alfred-banner__list">
							<li
								v-for="(note, i) in moduleValidationNotes"
								:key="i"
								:class="`alfred-module-note alfred-module-note--${note.severity || 'advisory'}`"
							>
								<strong>{{ (note.severity || 'advisory').toUpperCase() }}:</strong>
								{{ note.issue }}
								<span v-if="note.fix" class="alfred-module-note__fix">
									&#8594; {{ note.fix }}
								</span>
								<small class="alfred-module-note__source" v-if="note.source">
									({{ note.source }})
								</small>
							</li>
						</ul>
					</div>
				</div>
```

- [ ] **Step 5: Add CSS**

In the `<style scoped>` block (starts around line 997), add near the V1 `alfred-default-pill` rules:

```css
.alfred-module-badge {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 4px 10px;
	margin: 6px 0 10px;
	background: #f5f7fb;
	border: 1px solid #d7dde9;
	border-radius: 12px;
	font-size: 12px;
	color: #334;
}
.alfred-banner--module-notes {
	background: #fff7e6;
	border: 1px solid #ffdfa3;
}
.alfred-module-note--advisory { color: #444; }
.alfred-module-note--warning { color: #8a5a00; }
.alfred-module-note--blocker { color: #a11; font-weight: 500; }
.alfred-module-note__fix {
	display: block;
	margin-top: 2px;
	color: #556;
	font-size: 11px;
}
.alfred-module-note__source {
	margin-left: 6px;
	color: #889;
	font-size: 10px;
}
```

- [ ] **Step 6: Manual smoke test**

Restart `alfred-processing` with flags on: `cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing && ALFRED_PER_INTENT_BUILDERS=1 ALFRED_MODULE_SPECIALISTS=1 ./dev.sh`

Rebuild client: `bench build --app alfred_client`

Ask Alfred: *"Create a DocType called Accounts Voucher with fields voucher_date and voucher_amount."*

Expected:
- Module badge shows "Module context: accounts" (or "Accounts" once display plumbing is added — the key will appear until then).
- Validation-note banner renders with at least one advisory (missing Accounts Manager permission) if the LLM didn't include it, and a warning (submittable without GL hook) if `is_submittable=1`.
- With V2 flag off: badge and banner don't render; V1 default pills still work.

- [ ] **Step 7: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
git add alfred_client/public/js/alfred_chat/PreviewPanel.vue
git commit -m "feat(preview): render module badge and module validation notes"
```

---

## Self-Review

**Spec coverage (V2 spec sections -> tasks):**
- A. Module knowledge base -> Tasks 1, 2
- B. ModuleRegistry loader -> Task 3
- C. Module detection phase -> Task 5 (model + detect_module) + Task 11 (pipeline phase)
- D. Module specialist invocation -> Tasks 6, 7
- E. ValidationNote model -> Task 4
- F. Pipeline wiring -> Task 11
- G. Intent specialist prompt enhancement -> Tasks 8, 9
- H. Module-aware defaults in backfill -> Task 10
- I. `alfred_client` preview panel -> Task 12

**Error handling paths from spec (all covered):**
- Unknown module: Task 3 (loader returns None), Task 5 (detect fallback), Task 6 (rules return []), Task 7 (LLM wrappers return "" / []), Task 10 (backfill falls back to V1), Task 11 (phase no-ops).
- LLM failures: Task 5 (fallback), Task 7 (empty list / empty string), Task 11 (try/except in phase).
- Malformed JSON: Task 7 (`_parse_llm_note_list` robust fallback, rule notes still applied).
- Missing/invalid KB file: Task 3 (`json.loads` raises up to a test boundary; runtime loader skips silently — see loader implementation).
- Both flags off / V1 off: Task 11's no-op paths.

**Placeholder scan:** No "TBD", "TODO", "implement later" left. Two controlled references: Task 11 Step 10 says "locate the enclosing send call near line 1639" because V1's wire-up is already at that spot and exact line may drift. Task 12 Step 2 notes that `module_display_name` plumbing is deferred; the badge shows raw key until a future task plumbs display name (acceptable because V2 ships with one module and the raw key is already human-readable).

**Type consistency:**
- `ValidationNote` signature (`severity`, `source`, `issue`, `field`, `fix`, `changeset_index`) used identically in Tasks 4, 6, 7, 11, 12.
- `ModuleDecision` (module, reason, confidence, source) used identically in Tasks 5, 11.
- `module_context` string parameter named consistently in Tasks 7, 8, 9, 11.
- `ModuleRegistry` API (`.load()`, `.get()`, `.modules()`, `.for_doctype()`, `.detect()`) used consistently in Tasks 3, 5, 6, 7, 10, 11.
- `backfill_defaults_raw` signature `(changes, *, module=None)` used identically in Task 10 and Task 11.
- Pipeline context fields (`module`, `module_confidence`, `module_source`, `module_reason`, `module_context`, `module_validation_notes`) named identically in Tasks 11, 12.

**Gap check:** No spec requirement lacks a task. `_MODULE_CONTEXT_MARKER` idempotency from spec section G is covered by Task 8's idempotent test.
