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
		system = call_messages[0]["content"]
		assert "Accounts domain authority" in system
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
	assert "module_rule:accounts_submittable_needs_gl" in sources
	assert "module_rule:accounts_needs_accounts_manager_perm" in sources
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
	assert "module_rule:accounts_submittable_needs_gl" in sources
	assert not any(s.startswith("module_specialist:") for s in sources)
