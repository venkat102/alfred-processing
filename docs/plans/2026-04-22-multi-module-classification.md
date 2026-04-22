# Multi-Module Classification (V3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add multi-module classification (primary + up to 2 secondaries) gated by `ALFRED_MULTI_MODULE=1`, with deterministic merge rules for context, permissions, naming patterns, and validation notes.

**Architecture:** V2 single-module substrate is preserved. `ModuleRegistry.detect_all()` returns primary + secondaries from the heuristic. The pipeline's classify-module and provide-context phases fan out when the flag is on. Primary module's validation notes keep full severity; secondary modules' notes are capped at "warning" so they don't gate deploy. Primary wins naming pattern; permissions are unioned deduped across all modules.

**Tech Stack:** Python 3.11, pydantic v2, pytest asyncio, Vue 3 composition API. Tabs for Python, line 110.

**Repo:** `/Users/navin/office/frappe_bench/v16/mariadb/alfred-processing`
**Spec:** `docs/specs/2026-04-22-multi-module-classification.md`

---

## File Structure

**Modified files (alfred-processing):**
- `alfred/registry/module_loader.py` — add `detect_all()` method
- `alfred/orchestrator.py` — add `ModulesDecision` dataclass + `detect_modules()`; keep V2 `detect_module` as compat shim
- `alfred/handlers/post_build/backfill_defaults.py` — extend `backfill_defaults_raw` with `secondary_modules` kwarg
- `alfred/api/pipeline.py` — extend PipelineContext, rename+expand two phases, fan-out validation in post_crew, extend WebSocket payload

**Modified file (alfred_client):**
- `alfred_client/alfred_client/public/js/alfred_chat/PreviewPanel.vue` — badge "(with X)" and validation notes grouped by source

**New test files:**
- `tests/test_detect_modules.py`
- `tests/test_backfill_multi_module.py`
- `tests/test_pipeline_multi_module.py`

**Extended test files:**
- `tests/test_module_registry_loader.py` — `detect_all()` tests

---

## Task 1: `ModuleRegistry.detect_all()` returns primary + secondaries

- [ ] **Step 1: Write failing tests**

Append to `tests/test_module_registry_loader.py`:

```python
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
		prompt="totally unrelated greeting",
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
	# "journal entry" -> accounts (medium); "leave application" -> hr
	assert primary == "accounts"
	assert confidence == "medium"
	assert "hr" in secondaries
```

- [ ] **Step 2: Run — expect fail (AttributeError)**

- [ ] **Step 3: Implement `detect_all`**

Add to `alfred/registry/module_loader.py`:

```python
def detect_all(
	self, *, prompt: str, target_doctype: str | None, max_secondaries: int = 2,
) -> tuple[str | None, str, list[str]]:
	"""Return (primary, confidence, secondaries).

	Primary is chosen by target_doctype (high) or first keyword match
	(medium). Additional keyword matches past the primary become
	secondaries, up to max_secondaries, deduped against the primary.
	"""
	primary: str | None = None
	confidence: str = ""

	if target_doctype:
		kb = self._by_target_doctype.get(target_doctype)
		if kb is not None:
			primary = kb["module"]
			confidence = "high"

	low = (prompt or "").lower()
	keyword_hits: list[str] = []
	for kb in self._by_module.values():
		for kw in kb.get("detection_hints", {}).get("keyword_hints", []):
			if re.search(rf"\b{re.escape(kw.strip().lower())}\b", low):
				if kb["module"] not in keyword_hits:
					keyword_hits.append(kb["module"])
				break

	if primary is None and keyword_hits:
		primary = keyword_hits[0]
		confidence = "medium"

	secondaries = [m for m in keyword_hits if m != primary][:max_secondaries]
	return primary, confidence, secondaries
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add alfred/registry/module_loader.py tests/test_module_registry_loader.py
git commit -m "feat(module-registry): add detect_all for primary + secondary modules"
```

---

## Task 2: `ModulesDecision` + `detect_modules()` in orchestrator

- [ ] **Step 1: Write failing tests**

Create `tests/test_detect_modules.py`:

```python
from unittest.mock import AsyncMock, patch
import pytest
from alfred.orchestrator import ModulesDecision, detect_modules


@pytest.mark.asyncio
async def test_heuristic_primary_only():
	d = await detect_modules(
		prompt="Customize Sales Invoice",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert d.module == "accounts"
	assert d.secondary_modules == []
	assert d.source == "heuristic"
	assert d.confidence == "high"


@pytest.mark.asyncio
async def test_heuristic_primary_plus_secondary():
	d = await detect_modules(
		prompt="Sales Invoice that auto-creates a project task",
		target_doctype="Sales Invoice",
		site_config={},
	)
	assert d.module == "accounts"
	assert "projects" in d.secondary_modules
	assert d.source == "heuristic"


@pytest.mark.asyncio
async def test_heuristic_miss_llm_fallback_primary_only():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="accounts"),
	):
		d = await detect_modules(
			prompt="some vague request with no keyword hits",
			target_doctype=None,
			site_config={"llm_tier": "triage"},
		)
		assert d.module == "accounts"
		assert d.secondary_modules == []  # LLM never returns secondaries in V3
		assert d.source == "classifier"


@pytest.mark.asyncio
async def test_unknown_returns_empty_decision():
	with patch(
		"alfred.orchestrator._classify_module_llm",
		new=AsyncMock(return_value="unknown"),
	):
		d = await detect_modules(
			prompt="totally unrelated greeting",
			target_doctype=None,
			site_config={},
		)
		assert d.module is None
		assert d.secondary_modules == []
		assert d.source == "classifier"


def test_modules_decision_to_dict():
	d = ModulesDecision(
		module="accounts", secondary_modules=["projects"],
		reason="r", confidence="high", source="heuristic",
	)
	assert d.to_dict() == {
		"module": "accounts",
		"secondary_modules": ["projects"],
		"reason": "r",
		"confidence": "high",
		"source": "heuristic",
	}
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Add to `alfred/orchestrator.py` (append to the V2 intent/module block):**

```python
@dataclass
class ModulesDecision:
	"""V3 multi-module classification result.

	Mirrors ModuleDecision but adds ``secondary_modules``.
	"""

	module: str | None
	secondary_modules: list[str]
	reason: str
	confidence: str
	source: str

	def to_dict(self) -> dict:
		return {
			"module": self.module,
			"secondary_modules": list(self.secondary_modules),
			"reason": self.reason,
			"confidence": self.confidence,
			"source": self.source,
		}


async def detect_modules(
	*,
	prompt: str,
	target_doctype: str | None,
	site_config: dict,
) -> ModulesDecision:
	"""V3 heuristic + LLM fallback for primary + secondaries.

	Heuristic path uses ModuleRegistry.detect_all. LLM fallback is
	primary-only (secondaries stay empty) to avoid token budget blowup.
	"""
	registry = _ModuleRegistry.load()
	primary, confidence, secondaries = registry.detect_all(
		prompt=prompt, target_doctype=target_doctype,
	)
	if primary is not None:
		return ModulesDecision(
			module=primary,
			secondary_modules=secondaries,
			reason=f"matched heuristic ({confidence}) for {primary}; secondaries={secondaries}",
			confidence=confidence,
			source="heuristic",
		)

	try:
		tag = await _classify_module_llm(prompt, site_config)
		if tag == "unknown":
			return ModulesDecision(
				module=None, secondary_modules=[],
				reason="LLM classifier returned unknown",
				confidence="low", source="classifier",
			)
		return ModulesDecision(
			module=tag, secondary_modules=[],
			reason=f"LLM classifier returned {tag}",
			confidence="medium", source="classifier",
		)
	except Exception as e:
		logger.warning("Multi-module classifier failed: %s", e)
		return ModulesDecision(
			module=None, secondary_modules=[],
			reason=f"classifier error: {e}",
			confidence="low", source="fallback",
		)
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add alfred/orchestrator.py tests/test_detect_modules.py
git commit -m "feat(orchestrator): add ModulesDecision and detect_modules for multi-module"
```

---

## Task 3: Severity-capping helper in module_specialist

- [ ] **Step 1: Write failing test**

Append to `tests/test_module_specialist_rules.py`:

```python
from alfred.agents.specialists.module_specialist import cap_secondary_severity


def test_cap_secondary_severity_blocker_becomes_warning():
	from alfred.models.agent_outputs import ValidationNote
	notes = [
		ValidationNote(severity="blocker", source="module_rule:x", issue="a"),
		ValidationNote(severity="warning", source="module_rule:y", issue="b"),
		ValidationNote(severity="advisory", source="module_rule:z", issue="c"),
	]
	capped = cap_secondary_severity(notes)
	assert capped[0].severity == "warning"
	assert capped[1].severity == "warning"
	assert capped[2].severity == "advisory"
	# Original list unmodified
	assert notes[0].severity == "blocker"
```

- [ ] **Step 2: Run — expect ImportError**

- [ ] **Step 3: Implement in `alfred/agents/specialists/module_specialist.py`**

```python
def cap_secondary_severity(notes: list[ValidationNote]) -> list[ValidationNote]:
	"""Return copies with blocker severity capped to warning.

	Secondary modules cannot gate deploy - a blocker from a secondary
	context is at most a warning. Used by the V3 multi-module fan-out
	in the pipeline's validation post-pass.
	"""
	out: list[ValidationNote] = []
	for n in notes:
		if n.severity == "blocker":
			out.append(ValidationNote(
				severity="warning",
				source=n.source,
				issue=n.issue,
				field=n.field,
				fix=n.fix,
				changeset_index=n.changeset_index,
			))
		else:
			out.append(n)
	return out
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add alfred/agents/specialists/module_specialist.py tests/test_module_specialist_rules.py
git commit -m "feat(specialists): add cap_secondary_severity helper for multi-module fan-out"
```

---

## Task 4: Extend `backfill_defaults_raw` with `secondary_modules`

- [ ] **Step 1: Write failing tests**

Create `tests/test_backfill_multi_module.py`:

```python
from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw


def _dt(data):
	return {"op": "create", "doctype": "DocType", "data": data}


def test_secondary_modules_contribute_roles_deduped():
	# accounts (primary) + projects (secondary)
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	roles = {p["role"] for p in out[0]["data"]["permissions"]}
	# Primary contributes Accounts Manager/User; secondary contributes
	# Projects Manager/User + shared Employee
	assert "Accounts Manager" in roles
	assert "Accounts User" in roles
	assert "Projects Manager" in roles
	assert "Projects User" in roles
	assert "System Manager" in roles  # intent default


def test_primary_naming_wins_over_secondary():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["projects"],
	)
	# accounts naming is "format:ACC-..."; projects is "format:PRJ-..."
	assert out[0]["data"]["autoname"].startswith("format:ACC-")


def test_unknown_secondary_is_skipped():
	changes = [_dt({"name": "X", "module": "Custom"})]
	out = backfill_defaults_raw(
		changes, module="accounts", secondary_modules=["not_a_real_module"],
	)
	# Behaviour equivalent to single-module accounts
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
```

- [ ] **Step 2: Run — expect fail (TypeError / wrong perms)**

- [ ] **Step 3: Extend backfill**

In `alfred/handlers/post_build/backfill_defaults.py`:

```python
def backfill_defaults_raw(
	changes: list[dict],
	*,
	module: str | None = None,
	secondary_modules: list[str] | None = None,
) -> list[dict]:
	"""Raw-dict variant, V3-enabled.

	When ``module`` is set, primary module defaults layer on top of intent
	defaults (V2). When ``secondary_modules`` is also set, each one's
	``permissions_add_roles`` are appended (deduped by role). Primary
	module's naming pattern always wins; secondary modules never override
	naming.
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
			for sec in secondary_modules or []:
				backfilled = _apply_secondary_module_defaults(backfilled, sec)
		out.append(backfilled)
	return out


def _apply_secondary_module_defaults(change: dict, module: str) -> dict:
	"""Secondary modules contribute permission rows only; no naming swap."""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return change

	display_name = kb.get("display_name", module)
	conv = kb.get("conventions", {})
	new = {**change}
	data = copy.deepcopy(new.get("data") or {})
	meta = copy.deepcopy(new.get("field_defaults_meta") or {})

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
		addl = f"Added {', '.join(appended)} because request touches {display_name} as secondary context."
		meta["permissions"] = {
			"source": "default",
			"rationale": (prev_rationale + " " + addl).strip() if prev_rationale else addl,
		}
	new["data"] = data
	new["field_defaults_meta"] = meta
	return new
```

- [ ] **Step 4: Run — expect pass**

- [ ] **Step 5: Commit**

```bash
git add alfred/handlers/post_build/backfill_defaults.py tests/test_backfill_multi_module.py
git commit -m "feat(backfill): layer secondary module permissions on top of primary"
```

---

## Task 5: PipelineContext additions

- [ ] **Step 1: Extend PipelineContext in `alfred/api/pipeline.py`**

Below the V2 module fields, add:

```python
	# V3 multi-module additions. secondary_modules is populated only when
	# ALFRED_MULTI_MODULE=1. module_secondary_contexts maps module key ->
	# that module's provide_context snippet so the UI can attribute text
	# to its source.
	secondary_modules: list[str] = field(default_factory=list)
	module_secondary_contexts: dict[str, str] = field(default_factory=dict)
```

- [ ] **Step 2: Commit** (no tests for struct additions; covered by downstream tasks)

```bash
git add alfred/api/pipeline.py
git commit -m "feat(pipeline): add V3 multi-module PipelineContext fields"
```

---

## Task 6: Expand `_phase_classify_module` for V3 flag

- [ ] **Step 1: Write failing tests**

Create `tests/test_pipeline_multi_module.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from alfred.api.pipeline import AgentPipeline, PipelineContext


def _ctx(prompt: str) -> PipelineContext:
	conn = MagicMock()
	conn.site_config = {}
	c = PipelineContext(conn=conn, conversation_id="t", prompt=prompt)
	c.mode = "dev"
	return c


@pytest.mark.asyncio
async def test_classify_module_v3_flag_on_populates_secondaries(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("Sales Invoice that auto-creates a project task")
	p = AgentPipeline(c)
	await p._phase_classify_module()
	assert c.module == "accounts"
	assert "projects" in c.secondary_modules


@pytest.mark.asyncio
async def test_classify_module_v3_flag_off_keeps_v2_single_module(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.delenv("ALFRED_MULTI_MODULE", raising=False)
	c = _ctx("Sales Invoice that auto-creates a project task")
	p = AgentPipeline(c)
	await p._phase_classify_module()
	assert c.module == "accounts"
	assert c.secondary_modules == []  # V2 compat: no secondaries


@pytest.mark.asyncio
async def test_classify_module_v3_flag_on_but_no_secondary_keyword(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("customize the sales invoice form")
	p = AgentPipeline(c)
	await p._phase_classify_module()
	assert c.module == "accounts"
	assert c.secondary_modules == []
```

- [ ] **Step 2: Run — expect fail**

- [ ] **Step 3: Update `_phase_classify_module` in `pipeline.py`**

```python
async def _phase_classify_module(self) -> None:
	# ... same flag gating (V2 flags) as before ...
	if _os_for_flag.environ.get("ALFRED_MULTI_MODULE") == "1":
		from alfred.orchestrator import detect_modules
		targets = _extract_target_doctypes(ctx.prompt)
		first_target = targets[0] if targets else None
		decision = await detect_modules(
			prompt=ctx.prompt,
			target_doctype=first_target,
			site_config=ctx.conn.site_config or {},
		)
		ctx.module = decision.module
		ctx.secondary_modules = decision.secondary_modules
		ctx.module_source = decision.source
		ctx.module_confidence = decision.confidence
		ctx.module_reason = decision.reason
		ctx.module_target_doctype = first_target
		return

	# V2 path (unchanged)
	from alfred.orchestrator import detect_module
	targets = _extract_target_doctypes(ctx.prompt)
	first_target = targets[0] if targets else None
	decision = await detect_module(
		prompt=ctx.prompt,
		target_doctype=first_target,
		site_config=ctx.conn.site_config or {},
	)
	ctx.module = decision.module
	ctx.module_source = decision.source
	ctx.module_confidence = decision.confidence
	ctx.module_reason = decision.reason
	ctx.module_target_doctype = first_target
	# secondary_modules stays default []
```

- [ ] **Step 4: Run — expect pass. Commit.**

```bash
git add alfred/api/pipeline.py tests/test_pipeline_multi_module.py
git commit -m "feat(pipeline): classify_module fans out to secondaries when V3 flag on"
```

---

## Task 7: Expand `_phase_provide_module_context` for V3 fan-out

- [ ] **Step 1: Extend test file**

Append to `tests/test_pipeline_multi_module.py`:

```python
@pytest.mark.asyncio
async def test_provide_module_context_fans_out_to_secondaries(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("prompt")
	c.module = "accounts"
	c.secondary_modules = ["projects"]
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<ctx:{module}>"

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()
		assert "PRIMARY MODULE" in c.module_context
		assert "SECONDARY MODULE CONTEXT" in c.module_context
		assert "<ctx:accounts>" in c.module_context
		assert "<ctx:projects>" in c.module_context
		assert c.module_secondary_contexts == {"projects": "<ctx:projects>"}


@pytest.mark.asyncio
async def test_provide_module_context_secondary_llm_failure_silent(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("prompt")
	c.module = "accounts"
	c.secondary_modules = ["projects"]
	c.intent = "create_doctype"

	call_count = {"n": 0}
	async def fake_provide_context(*, module, **kwargs):
		call_count["n"] += 1
		if module == "projects":
			raise RuntimeError("boom")
		return "<ctx:accounts>"

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()
		assert "<ctx:accounts>" in c.module_context
		# Secondary failure is silent - projects section absent from merged
		assert "projects" not in c.module_secondary_contexts
```

- [ ] **Step 2: Update `_phase_provide_module_context` in `pipeline.py`**

```python
async def _phase_provide_module_context(self) -> None:
	# ... same flag gating (V2 flags) as before ...
	if not ctx.module:
		return
	from alfred.agents.specialists.module_specialist import provide_context
	from alfred.registry.module_loader import ModuleRegistry

	redis = getattr(getattr(ctx.conn, "websocket", None), "app", None)
	redis = getattr(getattr(redis, "state", None), "redis", None)

	registry = ModuleRegistry.load()

	def _display(m: str) -> str:
		try:
			return registry.get(m).get("display_name", m)
		except Exception:
			return m

	try:
		primary_ctx = await provide_context(
			module=ctx.module,
			intent=ctx.intent or "unknown",
			target_doctype=ctx.module_target_doctype,
			site_config=ctx.conn.site_config or {},
			redis=redis,
		)
	except Exception as e:
		logger.warning(
			"provide primary context failed for %s: %s", ctx.module, e,
		)
		primary_ctx = ""

	secondary_ctxs: dict[str, str] = {}
	for m in ctx.secondary_modules:
		try:
			snippet = await provide_context(
				module=m,
				intent=ctx.intent or "unknown",
				target_doctype=ctx.module_target_doctype,
				site_config=ctx.conn.site_config or {},
				redis=redis,
			)
			if snippet:
				secondary_ctxs[m] = snippet
		except Exception as e:
			logger.warning("provide secondary context failed for %s: %s", m, e)

	parts: list[str] = []
	if primary_ctx:
		parts.append(f"PRIMARY MODULE ({_display(ctx.module)}):\n{primary_ctx}")
	elif ctx.secondary_modules:
		# Primary contributes no context but we still flag it for the LLM
		parts.append(f"PRIMARY MODULE ({_display(ctx.module)}): (no context)")
	for m, s in secondary_ctxs.items():
		parts.append(f"SECONDARY MODULE CONTEXT ({_display(m)}):\n{s}")
	ctx.module_context = "\n\n".join(parts)
	ctx.module_secondary_contexts = secondary_ctxs
```

- [ ] **Step 3: Run — expect pass. Commit.**

```bash
git add alfred/api/pipeline.py tests/test_pipeline_multi_module.py
git commit -m "feat(pipeline): provide_module_context fans out to secondaries with clear headers"
```

---

## Task 8: Post-crew validation fan-out with severity capping + backfill secondaries

- [ ] **Step 1: Extend test file**

Append to `tests/test_pipeline_multi_module.py`:

```python
@pytest.mark.asyncio
async def test_post_crew_fans_out_validation_with_secondary_capping(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	from alfred.models.agent_outputs import ValidationNote
	c = _ctx("Sales Invoice that auto-creates a project task")
	c.module = "accounts"
	c.secondary_modules = ["projects"]
	c.intent = "create_doctype"
	c.changes = [
		{"op": "create", "doctype": "DocType", "data": {"name": "X", "is_submittable": 1}},
	]

	async def fake_validate(*, module, **kwargs):
		if module == "accounts":
			return [ValidationNote(severity="blocker", source="module_rule:a", issue="primary blocker")]
		return [ValidationNote(severity="blocker", source="module_rule:p", issue="secondary blocker")]

	with patch(
		"alfred.agents.specialists.module_specialist.validate_output",
		new=AsyncMock(side_effect=fake_validate),
	):
		sev_by_source = await _call_validation_fanout(c)
		assert sev_by_source["module_rule:a"] == "blocker"  # primary kept
		assert sev_by_source["module_rule:p"] == "warning"  # secondary capped


async def _call_validation_fanout(ctx):
	# Minimal driver: mirror what _phase_post_crew does for the fan-out
	# without standing up the full post_crew machinery (which also does
	# reflection, dry-run, rescue).
	from alfred.agents.specialists.module_specialist import (
		validate_output, cap_secondary_severity,
	)
	primary = await validate_output(
		module=ctx.module, intent=ctx.intent,
		changes=ctx.changes, site_config=ctx.conn.site_config or {},
	)
	secondary: list = []
	for m in ctx.secondary_modules:
		notes = await validate_output(
			module=m, intent=ctx.intent,
			changes=ctx.changes, site_config=ctx.conn.site_config or {},
		)
		secondary.extend(cap_secondary_severity(notes))
	return {n.source: n.severity for n in primary + secondary}
```

- [ ] **Step 2: Update `_phase_post_crew` in `pipeline.py`**

Locate the V2 block where `backfill_defaults_raw` is called and the `validate_output` block that follows. Replace with:

```python
# V3: pass secondary_modules through backfill when multi-module flag is on
if ctx.changes and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1":
	from alfred.handlers.post_build.backfill_defaults import backfill_defaults_raw
	try:
		module_arg = ctx.module if _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1" else None
		secondary_arg = (
			ctx.secondary_modules
			if _os_for_flag.environ.get("ALFRED_MULTI_MODULE") == "1"
			else []
		)
		ctx.changes = backfill_defaults_raw(
			ctx.changes, module=module_arg, secondary_modules=secondary_arg,
		)
	except Exception as e:
		logger.warning(
			"Defaults backfill failed for conversation=%s: %s",
			ctx.conversation_id, e, exc_info=True,
		)

# V2+V3: module specialist validation with primary full + secondaries capped
if (
	ctx.changes
	and ctx.module
	and _os_for_flag.environ.get("ALFRED_PER_INTENT_BUILDERS") == "1"
	and _os_for_flag.environ.get("ALFRED_MODULE_SPECIALISTS") == "1"
):
	from alfred.agents.specialists.module_specialist import (
		validate_output, cap_secondary_severity,
	)
	try:
		primary_notes = await validate_output(
			module=ctx.module, intent=ctx.intent or "unknown",
			changes=ctx.changes, site_config=ctx.conn.site_config or {},
		)
		secondary_notes: list = []
		if _os_for_flag.environ.get("ALFRED_MULTI_MODULE") == "1":
			for m in ctx.secondary_modules:
				try:
					notes = await validate_output(
						module=m, intent=ctx.intent or "unknown",
						changes=ctx.changes,
						site_config=ctx.conn.site_config or {},
					)
					secondary_notes.extend(cap_secondary_severity(notes))
				except Exception as e:
					logger.warning(
						"secondary validate for %s failed: %s", m, e,
					)
		all_notes = primary_notes + secondary_notes
		ctx.module_validation_notes = [n.model_dump() for n in all_notes]
	except Exception as e:
		logger.warning(
			"validate_output failed for conversation=%s module=%s: %s",
			ctx.conversation_id, ctx.module, e,
		)
		ctx.module_validation_notes = []
```

- [ ] **Step 3: Update WebSocket emit to include secondaries and confidence**

Locate the send-preview call (`type: "changeset"`). Extend:

```python
"data": {
	"conversation": ctx.conversation_id,
	"changes": ctx.changes,
	"result_text": ctx.result_text[:4000],
	"dry_run": ctx.dry_run_result,
	"module_validation_notes": ctx.module_validation_notes,
	"detected_module": ctx.module,
	"detected_module_secondaries": ctx.secondary_modules,
	"module_confidence": ctx.module_confidence,
},
```

- [ ] **Step 4: Run — expect pass. Commit.**

```bash
git add alfred/api/pipeline.py tests/test_pipeline_multi_module.py
git commit -m "feat(pipeline): post_crew fans out validation with secondary capping + emits secondaries in payload"
```

---

## Task 9: Client preview — badge "(with X)" and notes grouped by source

- [ ] **Step 1: Add computed for secondaries in PreviewPanel.vue**

```javascript
const detectedSecondaryModules = computed(() => {
	const raw = props.changeset?.detected_module_secondaries;
	if (!Array.isArray(raw)) return [];
	return raw;
});

const moduleBadgeLabel = computed(() => {
	if (!detectedModuleDisplay.value) return "";
	if (!detectedSecondaryModules.value.length) return detectedModuleDisplay.value;
	const joined = detectedSecondaryModules.value.join(", ");
	return `${detectedModuleDisplay.value} (with ${joined})`;
});

// Group validation notes by source module so the UI can label them.
// `source` is "module_rule:<rule_id>" or "module_specialist:<module>".
// We extract the module key from the source prefix.
const notesGroupedBySource = computed(() => {
	const groups = {};
	for (const n of moduleValidationNotes.value) {
		const src = n.source || "";
		let moduleKey = "unknown";
		if (src.startsWith("module_specialist:")) {
			moduleKey = src.slice("module_specialist:".length);
		} else if (src.startsWith("module_rule:")) {
			// rule ids are prefixed with their module (e.g. "accounts_submittable_needs_gl")
			const ruleId = src.slice("module_rule:".length);
			moduleKey = ruleId.split("_")[0];
		}
		if (!groups[moduleKey]) groups[moduleKey] = [];
		groups[moduleKey].push(n);
	}
	return groups;
});
```

- [ ] **Step 2: Update badge + notes rendering**

Replace badge text:

```vue
<div v-if="detectedModuleDisplay" class="alfred-module-badge">
	<span class="alfred-module-badge__icon" aria-hidden="true">&#9675;</span>
	<span class="alfred-module-badge__label">
		{{ __("Module context:") }} <strong>{{ moduleBadgeLabel }}</strong>
	</span>
</div>
```

Replace the module validation notes block with a grouped render:

```vue
<div
	v-if="previewState === 'PENDING' && moduleValidationNotes.length"
	class="alfred-banner alfred-banner--module-notes"
>
	<span class="alfred-banner__icon" aria-hidden="true">&#9873;</span>
	<div class="alfred-banner__body">
		<strong>{{ __("{0} module convention note(s)", [moduleValidationNotes.length]) }}</strong>
		<div v-for="(notes, moduleKey) in notesGroupedBySource" :key="moduleKey" class="alfred-module-note-group">
			<div class="alfred-module-note-group__header">
				<strong>{{ moduleKey }}</strong>
				<em v-if="detectedSecondaryModules.includes(moduleKey)" class="alfred-module-note-group__flag">
					{{ __("(secondary - advisory only)") }}
				</em>
			</div>
			<ul class="alfred-banner__list">
				<li
					v-for="(note, i) in notes"
					:key="i"
					:class="`alfred-module-note alfred-module-note--${note.severity || 'advisory'}`"
				>
					<strong>{{ (note.severity || 'advisory').toUpperCase() }}:</strong>
					{{ note.issue }}
					<span v-if="note.fix" class="alfred-module-note__fix">
						&#8594; {{ note.fix }}
					</span>
				</li>
			</ul>
		</div>
	</div>
</div>
```

- [ ] **Step 3: Add CSS**

```css
.alfred-module-note-group { margin-top: 6px; }
.alfred-module-note-group__header { font-size: 11px; color: #334; }
.alfred-module-note-group__flag {
	margin-left: 8px; color: #889; font-style: italic; font-size: 10px;
}
```

- [ ] **Step 4: Commit**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/v16_workbench/apps/alfred_client
git add alfred_client/public/js/alfred_chat/PreviewPanel.vue
git commit -m "feat(preview): multi-module badge and notes grouped by source module"
```

---

## Task 10: Full V1+V2+V3 regression + manual E2E checklist

- [ ] **Step 1: Full regression**

```bash
cd /Users/navin/office/frappe_bench/v16/mariadb/alfred-processing
.venv/bin/python -m pytest tests/ -q
```

Expected: all green, including V2 tests unchanged.

- [ ] **Step 2: Manual E2E on `dev.alfred`**

Enable all three flags:

```bash
ALFRED_PER_INTENT_BUILDERS=1 ALFRED_MODULE_SPECIALISTS=1 ALFRED_MULTI_MODULE=1 ./dev.sh
```

Rebuild client: `bench build --app alfred_client`

Test prompts:
- *"Create a DocType linking Employee to Salary Slip for a stipend calculation."* → expected primary=hr, secondary=[payroll], both permission sets merged.
- *"Create a Sales Invoice that posts a Project task on submit."* → expected primary=accounts, secondary=[projects].
- *"Add a custom field on Employee for emergency contact number."* → expected primary=hr, secondary=[] (no other module keyword hits).

Confirm in preview:
- Module badge shows primary plus "(with X)" when secondaries exist.
- Validation notes are grouped by source with "(secondary — advisory only)" flag on secondary-module groups.
- Blocker-from-primary disables Deploy; blocker-from-secondary (capped to warning) does NOT disable Deploy.

- [ ] **Step 3: Flag-off regression**

Run with `ALFRED_MULTI_MODULE` unset (V2 only): behaviour identical to before V3.

Run with both V2 and V3 unset (V1 only): behaviour identical to before V2.

---

## Self-Review

**Spec coverage:** Components A-H all mapped (detect_all → 1, ModulesDecision/detect_modules → 2, severity capping → 3, backfill → 4, PipelineContext → 5, classify phase → 6, provide-context phase → 7, post-crew + payload → 8, UI → 9, regression → 10).

**Placeholder scan:** no TBDs. "E2E" in Task 10 requires live stack - explicit; same shape as V2's E2E.

**Type consistency:** `ModulesDecision`, `detect_modules`, `detect_all`, `cap_secondary_severity`, `backfill_defaults_raw(secondary_modules=)`, `ctx.secondary_modules`, `ctx.module_secondary_contexts`, `module_confidence`, `detected_module_secondaries` — consistent across tasks.
