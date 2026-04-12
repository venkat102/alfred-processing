"""Tests for pure-function helpers in alfred.api.websocket.

Covers the non-async helpers that don't need a live WebSocket: changeset
extraction, tool-call description mapping, and the pipeline-mode precedence
resolution. The full pipeline is exercised in test_api_gateway.py / manual QA.
"""

import pytest

from alfred.api.websocket import _extract_changes, _describe_tool_call, _TOOL_ACTIVITY


class TestExtractChanges:
	"""Cover every shape the agent output can take."""

	def test_empty_string_returns_empty_list(self):
		assert _extract_changes("") == []

	def test_none_returns_empty_list(self):
		assert _extract_changes(None) == []

	def test_plain_json_array(self):
		text = '[{"op": "create", "doctype": "Notification", "data": {"name": "X"}}]'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["op"] == "create"
		assert result[0]["doctype"] == "Notification"
		assert result[0]["data"]["name"] == "X"

	def test_markdown_code_fence_json(self):
		"""Local LLMs wrap JSON in ```json...``` - must strip fences."""
		text = '```json\n[{"op": "create", "doctype": "Server Script", "data": {"name": "A"}}]\n```'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Server Script"

	def test_markdown_code_fence_without_language(self):
		text = '```\n[{"op": "create", "doctype": "Notification", "data": {"name": "B"}}]\n```'
		result = _extract_changes(text)
		assert len(result) == 1

	def test_object_with_plan_key(self):
		"""Architect sometimes returns {plan: [...]}."""
		text = '{"plan": [{"op": "create", "doctype": "Custom Field", "data": {"name": "Z"}}]}'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Custom Field"

	def test_object_with_customizations_needed_key(self):
		"""Requirement Analyst uses {customizations_needed: [...]}."""
		text = '{"customizations_needed": [{"type": "Notification", "name": "Alert", "description": "Notify user"}]}'
		result = _extract_changes(text)
		assert len(result) == 1
		# "type" maps to "doctype" in the normalizer
		assert result[0]["doctype"] == "Notification"

	def test_malformed_json_returns_empty(self):
		text = '{"plan": [garbage'
		assert _extract_changes(text) == []

	def test_non_dict_items_are_skipped(self):
		text = '[{"op": "create", "doctype": "A"}, "not a dict", {"op": "create", "doctype": "B"}]'
		result = _extract_changes(text)
		# Strings are skipped; only the two dicts make it through
		assert len(result) == 2
		assert result[0]["doctype"] == "A"
		assert result[1]["doctype"] == "B"

	def test_text_with_no_json_returns_empty(self):
		"""Plain English without any JSON at all."""
		text = "I have completed the task. The user should now review the changes."
		assert _extract_changes(text) == []

	def test_normalizes_top_level_name_into_data(self):
		"""Some agents put name at the top level instead of inside data."""
		text = '[{"op": "create", "doctype": "Notification", "name": "MyNotif", "data": {}}]'
		result = _extract_changes(text)
		assert result[0]["data"]["name"] == "MyNotif"

	def test_single_dict_wrapped_as_list(self):
		"""A standalone dict without an 'items' key is treated as a single item."""
		text = '{"op": "create", "doctype": "Notification", "data": {"name": "Solo"}}'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["data"]["name"] == "Solo"


class TestDescribeToolCall:
	"""The human-readable descriptions used by the activity ticker."""

	def test_known_tool_with_args(self):
		desc = _describe_tool_call("get_doctype_schema", {"doctype": "Leave Application"})
		assert "Leave Application" in desc
		assert "schema" in desc.lower()

	def test_known_tool_without_args(self):
		desc = _describe_tool_call("get_site_info", {})
		assert "site info" in desc.lower()

	def test_get_doctypes_with_module_filter(self):
		desc = _describe_tool_call("get_doctypes", {"module": "HR"})
		assert "HR" in desc

	def test_get_doctypes_without_module(self):
		desc = _describe_tool_call("get_doctypes", {})
		assert "doctypes" in desc.lower() or "DocTypes" in desc

	def test_check_permission_description(self):
		desc = _describe_tool_call(
			"check_permission", {"doctype": "Sales Invoice", "action": "write"}
		)
		assert "Sales Invoice" in desc
		assert "write" in desc

	def test_check_permission_default_action(self):
		desc = _describe_tool_call("check_permission", {"doctype": "User"})
		assert "read" in desc  # default action
		assert "User" in desc

	def test_dry_run_changeset_description(self):
		desc = _describe_tool_call("dry_run_changeset", {"changes": []})
		assert "validat" in desc.lower() or "dry" in desc.lower() or "live site" in desc.lower()

	def test_unknown_tool_falls_back_to_running(self):
		desc = _describe_tool_call("some_new_tool_we_havent_mapped_yet", {})
		assert "some_new_tool_we_havent_mapped_yet" in desc

	def test_unknown_tool_with_crashing_formatter(self):
		"""Defensive: if the formatter lambda raises (bad args shape), we
		should still return a description rather than crashing."""
		import alfred.api.websocket as ws_mod
		# Temporarily replace a formatter with one that crashes
		original = ws_mod._TOOL_ACTIVITY.get("get_site_info")
		ws_mod._TOOL_ACTIVITY["get_site_info"] = lambda a: 1 / 0
		try:
			desc = ws_mod._describe_tool_call("get_site_info", {})
			assert "get_site_info" in desc
		finally:
			if original is not None:
				ws_mod._TOOL_ACTIVITY["get_site_info"] = original

	def test_all_mapped_tools_have_string_descriptions(self):
		"""Every entry in _TOOL_ACTIVITY must produce a non-empty string when
		called with an empty args dict - guards against typos in the dict."""
		for name, formatter in _TOOL_ACTIVITY.items():
			desc = formatter({})
			assert isinstance(desc, str) and len(desc) > 0, (
				f"_TOOL_ACTIVITY[{name!r}] returned empty or non-string"
			)
