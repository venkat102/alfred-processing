"""Tests for the Phase 2 conversation memory layer.

Covers:
  - add_prompt / add_clarifications / add_changeset_items (happy path + caps)
  - render_for_prompt returns empty on empty memory, renders all three sections
    on a populated one
  - to_dict / from_dict round-trip preserves data
  - malformed changeset items are silently skipped
  - load returns a fresh ConversationMemory when no store or no data
  - save + load through a fake store returns equivalent memory
  - load tolerates store errors and bad JSON without raising
"""

import asyncio

from alfred.state.conversation_memory import (
	_MAX_CLARIFICATIONS,
	_MAX_ITEMS,
	_MAX_PROMPTS,
	ConversationMemory,
	load_conversation_memory,
	save_conversation_memory,
)


class _FakeStore:
	"""Tiny in-memory replacement for StateStore used across memory tests."""

	def __init__(self, raise_on=None):
		self._data: dict[tuple[str, str], dict] = {}
		self._raise_on = raise_on or set()

	async def get_task_state(self, site_id, key):
		if "get" in self._raise_on:
			raise RuntimeError("boom get")
		return self._data.get((site_id, key))

	async def set_task_state(self, site_id, key, data):
		if "set" in self._raise_on:
			raise RuntimeError("boom set")
		self._data[(site_id, key)] = data


class TestAddPrompt:
	def test_adds_prompt(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_prompt("Create a notification")
		assert m.recent_prompts == ["Create a notification"]

	def test_caps_prompts(self):
		m = ConversationMemory(conversation_id="c1")
		for i in range(_MAX_PROMPTS + 5):
			m.add_prompt(f"prompt {i}")
		assert len(m.recent_prompts) == _MAX_PROMPTS
		# Most recent kept, oldest dropped
		assert m.recent_prompts[-1] == f"prompt {_MAX_PROMPTS + 4}"
		assert m.recent_prompts[0] == "prompt 5"

	def test_empty_prompt_is_skipped(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_prompt("")
		assert m.recent_prompts == []


class TestAddClarifications:
	def test_appends(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_clarifications([("When to fire?", "On submit")])
		assert len(m.clarifications) == 1
		assert m.clarifications[0]["q"] == "When to fire?"
		assert m.clarifications[0]["a"] == "On submit"

	def test_caps_clarifications(self):
		m = ConversationMemory(conversation_id="c1")
		pairs = [(f"q{i}", f"a{i}") for i in range(_MAX_CLARIFICATIONS + 3)]
		m.add_clarifications(pairs)
		assert len(m.clarifications) == _MAX_CLARIFICATIONS
		assert m.clarifications[-1]["q"] == f"q{_MAX_CLARIFICATIONS + 2}"

	def test_skips_empty_pairs(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_clarifications([("", "something"), ("question?", ""), ("ok", "ok")])
		assert len(m.clarifications) == 1
		assert m.clarifications[0]["q"] == "ok"


class TestAddChangesetItems:
	def test_extracts_doctype_and_name(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "doctype": "Notification", "data": {"name": "Notify Approver"}},
		])
		assert len(m.items) == 1
		assert m.items[0]["doctype"] == "Notification"
		assert m.items[0]["name"] == "Notify Approver"
		assert m.items[0]["op"] == "create"

	def test_extracts_inner_doctype_when_outer_missing(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "data": {"doctype": "Server Script", "name": "x"}},
		])
		assert m.items[0]["doctype"] == "Server Script"

	def test_custom_field_captures_reference_doctype(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "doctype": "Custom Field",
			 "data": {"dt": "Sales Order", "fieldname": "priority", "label": "Priority"}},
		])
		# fieldname used as name when name missing
		assert m.items[0]["name"] == "priority"
		assert m.items[0]["on"] == "Sales Order"

	def test_server_script_captures_reference_doctype(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "doctype": "Server Script",
			 "data": {"reference_doctype": "Leave Application", "name": "validate_dates"}},
		])
		assert m.items[0]["on"] == "Leave Application"

	def test_notification_captures_document_type(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "doctype": "Notification",
			 "data": {"document_type": "Opportunity", "name": "Alert on Loss"}},
		])
		assert m.items[0]["on"] == "Opportunity"

	def test_malformed_items_are_silently_skipped(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			"not a dict",
			None,
			{},  # no doctype
			{"op": "create", "doctype": "Notification", "data": {"name": "Keep"}},
		])
		assert len(m.items) == 1
		assert m.items[0]["name"] == "Keep"

	def test_caps_items(self):
		m = ConversationMemory(conversation_id="c1")
		items = [
			{"op": "create", "doctype": "Notification", "data": {"name": f"N{i}"}}
			for i in range(_MAX_ITEMS + 5)
		]
		m.add_changeset_items(items)
		assert len(m.items) == _MAX_ITEMS
		# Oldest dropped
		assert m.items[-1]["name"] == f"N{_MAX_ITEMS + 4}"


class TestRenderForPrompt:
	def test_empty_memory_renders_empty_string(self):
		m = ConversationMemory(conversation_id="c1")
		assert m.render_for_prompt() == ""

	def test_renders_items_section(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_changeset_items([
			{"op": "create", "doctype": "Notification", "data": {"name": "Alert"}},
			{"op": "create", "doctype": "Custom Field",
			 "data": {"dt": "Sales Order", "fieldname": "priority"}},
		])
		out = m.render_for_prompt()
		assert "CONVERSATION CONTEXT" in out
		assert "Notification" in out
		assert "Alert" in out
		assert "Sales Order" in out
		assert "priority" in out

	def test_renders_clarifications(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_clarifications([("When?", "On submit")])
		out = m.render_for_prompt()
		assert "User decisions" in out
		assert "When?" in out
		assert "On submit" in out

	def test_renders_recent_prompts(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_prompt("first prompt")
		m.add_prompt("second prompt")
		out = m.render_for_prompt()
		assert "Recent prompts" in out
		assert "first prompt" in out
		assert "second prompt" in out

	def test_long_prompt_is_truncated(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_prompt("x" * 500)
		out = m.render_for_prompt()
		assert "..." in out
		assert "x" * 500 not in out


class TestAddInsightsQuery:
	def test_stores_qa_pair(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_insights_query("what DocTypes?", "You have 42 DocTypes.")
		assert len(m.insights_queries) == 1
		assert m.insights_queries[0]["q"] == "what DocTypes?"
		assert m.insights_queries[0]["a"] == "You have 42 DocTypes."

	def test_empty_question_or_answer_ignored(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_insights_query("", "ans")
		m.add_insights_query("q", "")
		assert m.insights_queries == []

	def test_long_answer_truncated(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_insights_query("q", "x" * 1000)
		assert len(m.insights_queries[0]["a"]) < 400
		assert m.insights_queries[0]["a"].endswith("...")

	def test_cap_enforced(self):
		m = ConversationMemory(conversation_id="c1")
		for i in range(20):
			m.add_insights_query(f"q{i}", f"a{i}")
		# Default cap is 10
		assert len(m.insights_queries) == 10
		# Oldest dropped, newest retained
		assert m.insights_queries[0]["q"] == "q10"
		assert m.insights_queries[-1]["q"] == "q19"


class TestRenderIncludesInsightsAndPlan:
	def test_insights_queries_rendered(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_insights_query("what workflows?", "You have 2: Leave and Expense.")
		text = m.render_for_prompt()
		assert "Insights-mode questions" in text
		assert "what workflows?" in text
		assert "Leave and Expense" in text

	def test_active_plan_rendered(self):
		m = ConversationMemory(conversation_id="c1")
		m.active_plan = {
			"title": "Approval workflow for Expense Claims",
			"status": "proposed",
			"summary": "Add a 2-step approval with manager then finance.",
		}
		text = m.render_for_prompt()
		assert "Active plan" in text
		assert "Approval workflow for Expense Claims" in text
		assert "proposed" in text
		assert "2-step approval" in text

	def test_proposed_plan_hides_steps(self):
		"""A proposed plan renders summary only - not the full step list.

		Rationale: the user hasn't approved yet, so we don't want the Dev
		crew to treat the plan as a spec if the orchestrator mis-routes a
		turn to Dev mode.
		"""
		m = ConversationMemory(conversation_id="c1")
		m.active_plan = {
			"title": "Proposed plan",
			"status": "proposed",
			"summary": "Plan summary.",
			"steps": [
				{"order": 1, "action": "Create Notification", "doctype": "Notification"},
			],
		}
		text = m.render_for_prompt()
		assert "Approved plan steps" not in text
		# But the title and summary should still be visible
		assert "Proposed plan" in text
		assert "Plan summary" in text

	def test_approved_plan_renders_full_step_list(self):
		"""An approved plan renders the full step list so the Dev enhancer
		sees them verbatim when injecting context.
		"""
		m = ConversationMemory(conversation_id="c1")
		m.active_plan = {
			"title": "Approved plan",
			"status": "approved",
			"summary": "Summary",
			"steps": [
				{"order": 1, "action": "Create Workflow 'Expense Approval'", "doctype": "Workflow", "rationale": "r1"},
				{"order": 2, "action": "Create Notification for approvers", "doctype": "Notification", "rationale": "r2"},
			],
			"doctypes_touched": ["Workflow", "Notification"],
		}
		text = m.render_for_prompt()
		assert "Approved plan steps" in text
		assert "Expense Approval" in text
		assert "approvers" in text
		assert "Workflow, Notification" in text

	def test_built_plan_status_hides_steps(self):
		"""A plan that's already been built must NOT re-inject its steps
		into future Dev-mode turns. Status 'built' = already consumed.
		"""
		m = ConversationMemory(conversation_id="c1")
		m.active_plan = {
			"title": "Old plan",
			"status": "built",
			"summary": "already built",
			"steps": [{"order": 1, "action": "Create Foo"}],
		}
		text = m.render_for_prompt()
		assert "Approved plan steps" not in text

	def test_empty_memory_still_returns_empty_string(self):
		m = ConversationMemory(conversation_id="c1")
		assert m.render_for_prompt() == ""


class TestAddPlanDocument:
	def test_sets_active_plan_and_appends(self):
		m = ConversationMemory(conversation_id="c1")
		plan = {"title": "P", "summary": "S", "steps": []}
		m.add_plan_document(plan, status="proposed")
		assert m.active_plan is not None
		assert m.active_plan["title"] == "P"
		assert m.active_plan["status"] == "proposed"
		assert len(m.plan_documents) == 1

	def test_cap_enforced(self):
		m = ConversationMemory(conversation_id="c1")
		for i in range(10):
			m.add_plan_document({"title": f"P{i}", "summary": "s"}, status="proposed")
		# Default cap is 5
		assert len(m.plan_documents) == 5
		assert m.plan_documents[0]["title"] == "P5"
		assert m.plan_documents[-1]["title"] == "P9"
		# active_plan is always the most recent
		assert m.active_plan["title"] == "P9"

	def test_ignores_empty_plan(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_plan_document({}, status="proposed")
		m.add_plan_document(None, status="proposed")  # type: ignore[arg-type]
		assert m.active_plan is None
		assert m.plan_documents == []

	def test_mark_active_plan_status(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_plan_document({"title": "P", "summary": "s"}, status="proposed")
		m.mark_active_plan_status("approved")
		assert m.active_plan["status"] == "approved"
		# The stored version should be in sync
		assert m.plan_documents[-1]["status"] == "approved"

	def test_mark_active_plan_noop_when_no_plan(self):
		m = ConversationMemory(conversation_id="c1")
		m.mark_active_plan_status("approved")  # should not raise
		assert m.active_plan is None


class TestSerialization:
	def test_round_trip(self):
		m = ConversationMemory(conversation_id="c1")
		m.add_prompt("Build a thing")
		m.add_clarifications([("When?", "On submit")])
		m.add_changeset_items([
			{"op": "create", "doctype": "Notification", "data": {"name": "N1"}},
		])
		m.add_insights_query("what workflows?", "You have 2.")
		m.add_plan_document({"title": "T", "summary": "sum"}, status="proposed")
		data = m.to_dict()
		restored = ConversationMemory.from_dict(data)
		assert restored.conversation_id == "c1"
		assert restored.recent_prompts == ["Build a thing"]
		assert restored.clarifications[0]["a"] == "On submit"
		assert restored.items[0]["name"] == "N1"
		assert restored.insights_queries[0]["q"] == "what workflows?"
		assert restored.active_plan["title"] == "T"
		assert len(restored.plan_documents) == 1

	def test_from_dict_tolerates_missing_fields(self):
		m = ConversationMemory.from_dict({})
		assert m.conversation_id == ""
		assert m.items == []
		assert m.clarifications == []
		assert m.recent_prompts == []
		assert m.insights_queries == []
		assert m.plan_documents == []
		assert m.active_plan is None

	def test_from_dict_tolerates_malformed_plan(self):
		m = ConversationMemory.from_dict({"active_plan": "not a dict"})
		assert m.active_plan is None


class TestLoadSaveIntegration:
	def test_load_with_no_store_returns_empty(self):
		result = asyncio.get_event_loop().run_until_complete(
			load_conversation_memory(None, "site1", "conv1")
		)
		assert isinstance(result, ConversationMemory)
		assert result.items == []
		assert result.conversation_id == "conv1"

	def test_load_returns_empty_when_not_in_store(self):
		store = _FakeStore()
		result = asyncio.get_event_loop().run_until_complete(
			load_conversation_memory(store, "site1", "conv1")
		)
		assert result.items == []

	def test_save_and_load_round_trip(self):
		store = _FakeStore()
		m = ConversationMemory(conversation_id="conv1")
		m.add_prompt("first")
		m.add_changeset_items([
			{"op": "create", "doctype": "Notification", "data": {"name": "Alert"}},
		])
		loop = asyncio.get_event_loop()
		loop.run_until_complete(save_conversation_memory(store, "site1", "conv1", m))
		restored = loop.run_until_complete(
			load_conversation_memory(store, "site1", "conv1")
		)
		assert restored.recent_prompts == ["first"]
		assert restored.items[0]["name"] == "Alert"

	def test_load_tolerates_store_errors(self):
		store = _FakeStore(raise_on={"get"})
		result = asyncio.get_event_loop().run_until_complete(
			load_conversation_memory(store, "site1", "conv1")
		)
		assert result.items == []
		assert result.conversation_id == "conv1"

	def test_save_tolerates_store_errors(self):
		store = _FakeStore(raise_on={"set"})
		m = ConversationMemory(conversation_id="conv1")
		m.add_prompt("x")
		# Should not raise
		asyncio.get_event_loop().run_until_complete(
			save_conversation_memory(store, "site1", "conv1", m)
		)

	def test_save_is_noop_for_missing_store(self):
		# Should not raise
		m = ConversationMemory(conversation_id="conv1")
		asyncio.get_event_loop().run_until_complete(
			save_conversation_memory(None, "site1", "conv1", m)
		)
