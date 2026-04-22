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
	monkeypatch.delenv("ALFRED_MULTI_MODULE", raising=False)
	c = _ctx("prompt")
	c.module = "accounts"
	c.secondary_modules = ["projects"]  # populated by something else; should be ignored
	c.intent = "create_doctype"

	async def fake_provide_context(*, module, **kwargs):
		return f"<ctx:{module}>"

	with patch(
		"alfred.agents.specialists.module_specialist.provide_context",
		new=AsyncMock(side_effect=fake_provide_context),
	) as spy:
		p = AgentPipeline(c)
		await p._phase_provide_module_context()
		# With V3 flag off, only the primary call fires
		assert spy.await_count == 1
		assert c.module_secondary_contexts == {}
