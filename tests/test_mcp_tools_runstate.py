"""Tests for the Phase 1 tool-usage improvements in `_mcp_call`.

Covers:
  - init_run_state sets up the tracking dict
  - Call budget (P2): hard cap triggers budget_exceeded error
  - Dedup cache (P4): same (tool, args) returns cached result
  - Failure counter (A2): error responses tracked and surfaced
  - Misuse warning (A3): dry_run before schema lookup adds a hint
  - No run_state => wrapper degrades gracefully (old behavior)

These tests use a fake MCPClient with a canned call_sync so we don't need
a live client connection.
"""

import json

from alfred.tools.mcp_tools import (
	DEFAULT_CALL_BUDGET,
	_mcp_call,
	init_run_state,
)


class FakeMCPClient:
	"""Minimal MCP client stand-in. Records calls, returns canned responses."""

	def __init__(self, canned_response=None, raises=None):
		self.canned_response = canned_response or {"ok": True}
		self.raises = raises
		self.call_log = []

	def call_sync(self, tool_name, arguments=None, timeout=None):
		self.call_log.append((tool_name, arguments))
		if self.raises:
			raise self.raises
		return self.canned_response


class TestInitRunState:
	def test_sets_attribute(self):
		client = FakeMCPClient()
		init_run_state(client, conversation_id="conv-1")
		assert hasattr(client, "run_state")
		assert client.run_state["conversation_id"] == "conv-1"
		assert client.run_state["call_budget"] == DEFAULT_CALL_BUDGET
		assert client.run_state["calls_made"] == 0

	def test_reset_clears_dedup(self):
		client = FakeMCPClient()
		init_run_state(client, conversation_id="conv-1")
		client.run_state["dedup_cache"]["key"] = "stale"
		init_run_state(client, conversation_id="conv-2")
		# After re-init, dedup cache should be empty
		assert client.run_state["dedup_cache"] == {}
		assert client.run_state["conversation_id"] == "conv-2"


class TestCallBudget:
	def test_under_budget_passes(self):
		client = FakeMCPClient(canned_response={"fields": []})
		init_run_state(client)
		# Unique args per call so dedup doesn't kick in
		for i in range(5):
			result_json = _mcp_call(client, "get_doctype_schema", {"doctype": f"Foo{i}"})
			result = json.loads(result_json)
			assert "error" not in result or result.get("error") != "budget_exceeded"
		assert client.run_state["calls_made"] == 5

	def test_budget_exceeded_blocks_further_calls(self):
		client = FakeMCPClient(canned_response={"fields": []})
		init_run_state(client)
		client.run_state["call_budget"] = 3
		# Three calls burn the budget
		for _ in range(3):
			_mcp_call(client, "get_site_info", {"nonce": _})  # unique args to skip dedup
		# Fourth should fail loud
		result_json = _mcp_call(client, "get_site_info", {"nonce": 999})
		result = json.loads(result_json)
		assert result.get("error") == "budget_exceeded"
		# No extra call_sync invocations after exceeding budget
		assert len(client.call_log) == 3


class TestDedup:
	def test_same_call_returns_cached(self):
		client = FakeMCPClient(canned_response={"doctype": "Sales Order", "fields": [1, 2, 3]})
		init_run_state(client)
		first = _mcp_call(client, "get_doctype_schema", {"doctype": "Sales Order"})
		second = _mcp_call(client, "get_doctype_schema", {"doctype": "Sales Order"})
		assert first == second
		# Only ONE call_sync invocation - second was a cache hit
		assert len(client.call_log) == 1
		assert client.run_state["dedup_hits"] == 1

	def test_different_args_not_deduped(self):
		client = FakeMCPClient(canned_response={"doctype": "X"})
		init_run_state(client)
		_mcp_call(client, "get_doctype_schema", {"doctype": "Sales Order"})
		_mcp_call(client, "get_doctype_schema", {"doctype": "Purchase Order"})
		assert len(client.call_log) == 2
		assert client.run_state["dedup_hits"] == 0

	def test_different_tool_not_deduped(self):
		client = FakeMCPClient(canned_response={"ok": True})
		init_run_state(client)
		_mcp_call(client, "get_site_info", {})
		_mcp_call(client, "get_user_context", {})
		assert len(client.call_log) == 2

	def test_dedup_insensitive_to_dict_key_order(self):
		"""Same args in different key order must dedup. If the agent emits
		{"a": 1, "b": 2} on one call and {"b": 2, "a": 1} on the next, they
		are semantically identical and must hit the cache."""
		client = FakeMCPClient(canned_response={"fields": []})
		init_run_state(client)
		_mcp_call(client, "get_doctype_schema", {"doctype": "Sales Order", "include_fields": True})
		_mcp_call(client, "get_doctype_schema", {"include_fields": True, "doctype": "Sales Order"})
		assert len(client.call_log) == 1, (
			"Dedup must be insensitive to argument dict key order"
		)
		assert client.run_state["dedup_hits"] == 1

	def test_cached_call_does_not_consume_budget(self):
		"""A cache hit must not count against the call budget - otherwise
		a tight dedup cache could starve the agent artificially."""
		client = FakeMCPClient(canned_response={"ok": True})
		init_run_state(client, budget=3)
		_mcp_call(client, "get_site_info", {})   # 1 real call
		_mcp_call(client, "get_site_info", {})   # cached
		_mcp_call(client, "get_site_info", {})   # cached
		_mcp_call(client, "get_site_info", {})   # cached
		assert len(client.call_log) == 1
		assert client.run_state["calls_made"] == 1
		# Budget is not exhausted - three more fresh calls still work
		_mcp_call(client, "get_doctypes", {})
		_mcp_call(client, "get_user_context", {})
		assert client.run_state["calls_made"] == 3


class TestFailureCounter:
	def test_error_response_counted(self):
		client = FakeMCPClient(canned_response={
			"error": "permission_denied",
			"message": "forbidden",
		})
		init_run_state(client)
		_mcp_call(client, "get_doctype_schema", {"doctype": "Restricted"})
		assert client.run_state["failure_count"] == 1
		assert len(client.run_state["failures"]) == 1
		assert client.run_state["failures"][0] == ("get_doctype_schema", "permission_denied")

	def test_failures_surfaced_in_next_response(self):
		client = FakeMCPClient()
		init_run_state(client)
		# First call: fails
		client.canned_response = {"error": "permission_denied", "message": "nope"}
		_mcp_call(client, "get_doctype_schema", {"doctype": "Restricted"})
		# Second call: succeeds but should include the failure hint
		client.canned_response = {"doctypes": []}
		result_json = _mcp_call(client, "get_doctypes", {})
		result = json.loads(result_json)
		notes = result.get("_alfred_notes", [])
		assert any("Previous failures" in note for note in notes), \
			f"Expected failure hint in notes, got: {notes}"

	def test_timeout_error_counted(self):
		client = FakeMCPClient(raises=TimeoutError("client timeout"))
		init_run_state(client)
		result_json = _mcp_call(client, "get_site_info")
		result = json.loads(result_json)
		assert result.get("error") == "timeout"
		assert client.run_state["failure_count"] == 1


class TestMisuseWarning:
	def test_dry_run_without_prior_lookup_gets_warning(self):
		client = FakeMCPClient(canned_response={"valid": True, "issues": []})
		init_run_state(client)
		result_json = _mcp_call(client, "dry_run_changeset", {"changes": "[]"})
		result = json.loads(result_json)
		notes = result.get("_alfred_notes", [])
		assert any("before any schema lookup" in note for note in notes), \
			f"Expected misuse warning, got: {notes}"

	def test_dry_run_after_lookup_no_warning(self):
		client = FakeMCPClient(canned_response={"fields": []})
		init_run_state(client)
		# First: legitimate schema lookup
		_mcp_call(client, "lookup_doctype", {"name": "Sales Order"})
		# Then: dry_run - should NOT get the misuse warning
		client.canned_response = {"valid": True, "issues": []}
		result_json = _mcp_call(client, "dry_run_changeset", {"changes": "[]"})
		result = json.loads(result_json)
		notes = result.get("_alfred_notes", [])
		misuse_hints = [n for n in notes if "before any schema lookup" in n]
		assert not misuse_hints, f"Expected no misuse warning, got: {misuse_hints}"


class TestGracefulDegradation:
	def test_no_run_state_returns_normal_result(self):
		"""When mcp_client has no run_state attribute, the wrapper should still work."""
		client = FakeMCPClient(canned_response={"doctypes": ["Sales Order"]})
		# Deliberately NOT calling init_run_state
		result_json = _mcp_call(client, "get_doctypes", {})
		result = json.loads(result_json)
		assert result == {"doctypes": ["Sales Order"]}
		assert len(client.call_log) == 1

	def test_no_run_state_no_dedup(self):
		"""Without run_state, every call round-trips even if identical."""
		client = FakeMCPClient(canned_response={"ok": True})
		_mcp_call(client, "get_site_info", {})
		_mcp_call(client, "get_site_info", {})
		assert len(client.call_log) == 2
