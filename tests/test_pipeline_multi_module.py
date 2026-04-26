from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from alfred.api.pipeline import AgentPipeline, PipelineContext


@pytest.fixture(autouse=True)
def _reset_settings_cache():
	from alfred.config import get_settings
	get_settings.cache_clear()
	yield
	get_settings.cache_clear()


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
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "0")
	c = _ctx("Sales Invoice that auto-creates a project task")
	p = AgentPipeline(c)
	await p._phase_classify_module()
	assert c.module == "accounts"
	assert c.secondary_modules == []


@pytest.mark.asyncio
async def test_classify_module_v3_no_secondary_keyword(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("customize the sales invoice form")
	p = AgentPipeline(c)
	await p._phase_classify_module()
	assert c.module == "accounts"
	assert c.secondary_modules == []


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
async def test_provide_module_context_secondary_failure_silent(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("prompt")
	c.module = "accounts"
	c.secondary_modules = ["projects"]
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
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
		assert "projects" not in c.module_secondary_contexts


@pytest.mark.asyncio
async def test_provide_module_context_flag_off_does_not_fan_out(monkeypatch):
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "0")
	c = _ctx("prompt")
	c.module = "accounts"
	c.secondary_modules = ["projects"]  # populated by something else; should be ignored
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<ctx:{module}>"

	async def fake_family_context(*, family, **kwargs):
		return ""  # disable family context in this isolated test

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	) as spy, patch(
		"alfred.agents.specialists.module_specialist.provide_family_context",
		new=AsyncMock(side_effect=fake_family_context),
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()
		# With V3 flag off, only the primary module call fires
		assert spy.await_count == 1
		assert c.module_secondary_contexts == {}


@pytest.mark.asyncio
async def test_provide_module_context_emits_primary_family_header(monkeypatch):
	"""Family layer: when V3 is on and the primary module has a family,
	the assembled module_context starts with a PRIMARY FAMILY section
	naming the family's display_name, followed by PRIMARY MODULE, then
	SECONDARY MODULE sections."""
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("prompt")
	c.module = "accounts"  # Transactions family
	c.secondary_modules = ["stock"]  # Operations family - different, so should emit
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<module:{module}>"

	async def fake_family_context(*, family, **kwargs):
		return f"<family:{family}>"

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	), patch(
		"alfred.agents.specialists.module_specialist.provide_family_context",
		new=AsyncMock(side_effect=fake_family_context),
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()

	# Order: PRIMARY FAMILY -> PRIMARY MODULE -> SECONDARY MODULE
	idx_family = c.module_context.index("PRIMARY FAMILY (Transactions)")
	idx_module = c.module_context.index("PRIMARY MODULE (Accounts)")
	idx_secondary = c.module_context.index("SECONDARY MODULE CONTEXT (Stock)")
	assert idx_family < idx_module < idx_secondary
	assert "<family:transactions>" in c.module_context
	assert "<module:accounts>" in c.module_context
	assert "<module:stock>" in c.module_context


@pytest.mark.asyncio
async def test_provide_module_context_familyless_custom_skips_family_section(monkeypatch):
	"""Family layer: custom has no family - no PRIMARY FAMILY section emitted."""
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "1")
	c = _ctx("prompt")
	c.module = "custom"
	c.secondary_modules = []
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<module:{module}>"

	family_spy = AsyncMock(return_value="<should not be called>")
	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	), patch(
		"alfred.agents.specialists.module_specialist.provide_family_context",
		new=family_spy,
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()

	assert "PRIMARY FAMILY" not in c.module_context
	assert "PRIMARY MODULE (Custom)" in c.module_context
	# family context was never asked for - custom is familyless
	assert family_spy.await_count == 0


@pytest.mark.asyncio
async def test_provide_module_context_family_section_in_v2_fallback(monkeypatch):
	"""V2 path (MULTI_MODULE flag off): family context is still prepended
	inline as "FAMILY CONTEXT (X): ..." so single-module callers also
	benefit from cross-module invariants."""
	monkeypatch.setenv("ALFRED_PER_INTENT_BUILDERS", "1")
	monkeypatch.setenv("ALFRED_MODULE_SPECIALISTS", "1")
	monkeypatch.setenv("ALFRED_MULTI_MODULE", "0")
	c = _ctx("prompt")
	c.module = "payroll"  # People family
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<module:{module}>"

	async def fake_family_context(*, family, **kwargs):
		return f"<family:{family}>"

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	), patch(
		"alfred.agents.specialists.module_specialist.provide_family_context",
		new=AsyncMock(side_effect=fake_family_context),
	):
		p = AgentPipeline(c)
		await p._phase_provide_module_context()

	assert "FAMILY CONTEXT (People)" in c.module_context
	assert "<family:people>" in c.module_context
	assert "<module:payroll>" in c.module_context
