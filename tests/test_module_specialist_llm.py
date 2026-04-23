import json
from unittest.mock import AsyncMock, patch

import pytest

from alfred.agents.specialists.module_specialist import (
	_context_cache_clear,
	provide_context,
	validate_output,
)


@pytest.fixture(autouse=True)
def _reset_cache():
	_context_cache_clear()
	yield
	_context_cache_clear()


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
				"data": {"name": "Voucher", "is_submittable": 1, "module": "Accounts"},
			}],
			site_config={},
		)
	sources = {n.source for n in notes}
	assert "module_rule:accounts_submittable_non_posting_doctype" in sources
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
				"data": {"name": "Voucher", "is_submittable": 1, "module": "Accounts"},
			}],
			site_config={},
		)
	sources = {n.source for n in notes}
	assert "module_rule:accounts_submittable_non_posting_doctype" in sources
	assert not any(s.startswith("module_specialist:") for s in sources)


@pytest.mark.asyncio
async def test_validate_output_dedups_llm_note_that_matches_rule_note():
	# The rule runner will produce the canonical submittable-non-posting
	# advisory message. The LLM returns the SAME message verbatim - it
	# should be deduped so the user only sees one note.
	rule_message = (
		"New submittable Accounts DocType detected. Only Sales Invoice, "
		"Purchase Invoice, Journal Entry, and Payment Entry post to GL on "
		"submit in standard ERPNext. If this DocType is meant to post GL "
		"entries, add an explicit Server Script with doctype_event='on_submit'."
	)
	llm_reply = json.dumps([
		{"severity": "advisory", "issue": rule_message},
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
				"data": {"name": "Voucher", "is_submittable": 1, "module": "Accounts"},
			}],
			site_config={},
		)
	# Only ONE note with that message - the rule's copy, not the LLM's.
	matching = [n for n in notes if "submit in standard ERPNext" in n.issue]
	assert len(matching) == 1
	assert matching[0].source.startswith("module_rule:")


@pytest.mark.asyncio
async def test_provide_context_caches_within_ttl():
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="cached snippet"),
	) as llm:
		first = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		second = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		assert first == second == "cached snippet"
		# Second call should hit cache, not LLM
		assert llm.await_count == 1


@pytest.mark.asyncio
async def test_provide_context_cache_keyed_on_all_three_inputs():
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(side_effect=["first", "second", "third"]),
	) as llm:
		await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		# Different target_doctype -> cache miss
		await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Journal Entry", site_config={},
		)
		# Different intent -> cache miss
		await provide_context(
			module="accounts", intent="create_server_script",
			target_doctype="Sales Invoice", site_config={},
		)
		assert llm.await_count == 3


@pytest.mark.asyncio
async def test_provide_context_empty_reply_not_cached():
	# Don't cache empty results - next caller should retry
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(side_effect=["", "real snippet"]),
	) as llm:
		first = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		second = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		assert first == ""
		assert second == "real snippet"
		assert llm.await_count == 2


def _mock_redis():
	"""In-memory stand-in for aioredis.Redis.

	Exposes the two methods provide_context uses (``get``, ``setex``) as
	AsyncMocks that talk to a dict. Good enough for cache-behaviour tests
	without spinning up a real Redis.
	"""
	store: dict[str, str] = {}

	async def _get(key):
		return store.get(key)

	async def _setex(key, ttl, value):
		store[key] = value

	redis = AsyncMock()
	redis.get = AsyncMock(side_effect=_get)
	redis.setex = AsyncMock(side_effect=_setex)
	redis._store = store  # test-visible handle
	return redis


@pytest.mark.asyncio
async def test_provide_context_uses_redis_when_provided():
	redis = _mock_redis()
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="from redis write"),
	) as llm:
		first = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={}, redis=redis,
		)
		# First call: LLM hit, Redis SETEX
		assert first == "from redis write"
		assert llm.await_count == 1
		assert redis.setex.await_count == 1
		key = "alfred:module_ctx:accounts:create_doctype:Sales Invoice"
		assert redis._store[key] == "from redis write"


@pytest.mark.asyncio
async def test_provide_context_redis_hit_skips_llm():
	redis = _mock_redis()
	# Seed Redis directly as though another worker wrote earlier.
	await redis.setex(
		"alfred:module_ctx:accounts:create_doctype:Sales Invoice",
		300, "cross-worker cached",
	)
	_context_cache_clear()  # ensure in-memory doesn't shadow the test
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="should not be called"),
	) as llm:
		result = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={}, redis=redis,
		)
		assert result == "cross-worker cached"
		assert llm.await_count == 0


@pytest.mark.asyncio
async def test_provide_context_redis_failure_falls_back_to_llm_and_inmem():
	# Redis .get raises -> should transparently fall through to the LLM call
	# and then populate the in-memory cache.
	redis = AsyncMock()
	redis.get = AsyncMock(side_effect=RuntimeError("redis down"))
	redis.setex = AsyncMock(side_effect=RuntimeError("redis still down"))
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="fallback ok"),
	) as llm:
		out = await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={}, redis=redis,
		)
		assert out == "fallback ok"
		assert llm.await_count == 1


@pytest.mark.asyncio
async def test_provide_context_without_redis_uses_inmem_cache():
	# No redis kwarg -> pure in-memory path; a second call hits cache.
	with patch(
		"alfred.agents.specialists.module_specialist._ollama_chat",
		new=AsyncMock(return_value="inmem only"),
	) as llm:
		await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		await provide_context(
			module="accounts", intent="create_doctype",
			target_doctype="Sales Invoice", site_config={},
		)
		assert llm.await_count == 1
