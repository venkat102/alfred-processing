"""Tests for pure-function helpers in alfred.api.websocket.

Covers the non-async helpers that don't need a live WebSocket: changeset
extraction, tool-call description mapping, and the pipeline-mode precedence
resolution. The full pipeline is exercised in test_api_gateway.py / manual QA.
"""

import pytest

from alfred.api.websocket import (
	_extract_changes, _describe_tool_call, _TOOL_ACTIVITY, _validate_changeset_shape,
)


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

	def test_prose_with_stray_example_doc_json_is_rejected(self):
		"""Drift guard: agent writes documentation prose, then pastes an
		example 'how to create <doctype>' snippet at the end. The trailing
		JSON object has `doctype` and `customer` but NO `op`/`operation`
		and no nested `data` dict - it's a user-facing example, not a
		changeset item. Must NOT be coerced into a create operation.

		Regression for the Employee-validation prompt that produced a
		Sales Order documentation dump with this trailing example.
		"""
		text = (
			"### Document Type: Sales Order\n\n"
			"- **Module**: Selling - part of the Selling module.\n"
			"- **Is Single**: 0 - multiple instances allowed.\n\n"
			"### Example Usage\n\n"
			"To create a new Sales Order in ERPNext, you would use:\n\n"
			'```json\n'
			'{\n'
			'  "doctype": "Sales Order",\n'
			'  "customer": "CUST-001",\n'
			'  "items": [{"item_code": "ITEM-001", "qty": 10, "rate": 50.00}],\n'
			'  "taxes_and_charges": "TAX-001"\n'
			'}\n'
			'```\n\n'
			"This JSON would be sent to the API to create a new Sales Order."
		)
		result = _extract_changes(text)
		# Must return empty so the rescue path can regenerate from the
		# original user prompt (which was about Employee validation, not
		# Sales Order creation)
		assert result == []

	def test_bare_dict_without_op_or_doctype_is_rejected(self):
		"""Another drift variant: the agent outputs a dict with field
		values but no op/doctype metadata. Not a changeset item.
		"""
		text = '{"customer": "CUST-001", "amount": 100.0, "items": []}'
		result = _extract_changes(text)
		assert result == []

	def test_bare_dict_with_doctype_but_no_data_is_rejected(self):
		"""A dict with just a top-level `doctype` but no `op` and no `data`
		sub-dict is a description, not a changeset item.
		"""
		text = '{"doctype": "Sales Order", "description": "Records a sale"}'
		result = _extract_changes(text)
		assert result == []

	def test_bare_dict_with_op_and_doctype_passes(self):
		"""Sanity: a proper single-item dict with op/doctype/data still parses."""
		text = '{"op": "create", "doctype": "Notification", "data": {"name": "N1"}}'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Notification"

	def test_bare_dict_with_doctype_and_data_passes(self):
		"""Sanity: a dict with doctype + nested data (but no explicit op)
		is treated as an implicit create - same behavior as before the
		drift guard.
		"""
		text = '{"doctype": "Notification", "data": {"name": "N1", "subject": "Hi"}}'
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Notification"
		assert result[0]["data"]["name"] == "N1"

	def test_python_dict_repr_with_single_quotes(self):
		"""LLMs occasionally emit Python dict repr (single quotes, True/None)
		instead of strict JSON. ast.literal_eval should rescue it."""
		text = "[{'op': 'create', 'doctype': 'Notification', 'data': {'name': 'X', 'enabled': True}}]"
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Notification"
		assert result[0]["data"]["name"] == "X"
		assert result[0]["data"]["enabled"] is True

	def test_python_dict_repr_with_plan_key(self):
		"""Same dict-repr fallback but inside a {plan: [...]} wrapper."""
		text = "{'plan': [{'op': 'create', 'doctype': 'DocType', 'name': 'Alfred_Notification'}], 'approval': 'approved'}"
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "DocType"
		assert result[0]["data"]["name"] == "Alfred_Notification"

	def test_non_string_input_is_coerced(self):
		"""If a caller accidentally passes a dict/list instead of a string, coerce
		to str() rather than crashing."""
		data = [{"op": "create", "doctype": "Notification", "data": {"name": "Y"}}]
		result = _extract_changes(data)
		assert len(result) == 1
		assert result[0]["data"]["name"] == "Y"

	def test_repeated_concatenated_json_blocks_picks_first(self):
		"""Qwen retry loops produce the same JSON array 5+ times in one Final Answer.
		The greedy regex version choked on this with 'Extra data' - raw_decode
		should pick the first complete block and ignore the trailing junk."""
		one_block = '[{"op": "create", "doctype": "Workflow", "data": {"name": "LAW"}}]'
		text = (
			"Thought: Let me fix the issue.\n\n"
			f"```json\n{one_block}\n```\n\n"
			"Let's now create the corrected changeset.\n\n"
			f"```json\n{one_block}\n```\n\n"
			"Here is the final version:\n\n"
			f"```json\n{one_block}\n```"
		)
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Workflow"
		assert result[0]["data"]["name"] == "LAW"

	def test_qwen_chat_template_tokens_stripped(self):
		"""qwen2.5-coder sometimes leaks `<|im_start|>` tokens into the Final
		Answer when max_tokens is too generous. Those must not break parsing."""
		text = (
			'<|im_start|>Here is the changeset:\n\n'
			'```json\n'
			'[{"op": "create", "doctype": "Notification", "data": {"name": "Alert"}}]\n'
			'```\n<|im_end|><|im_start|>'
		)
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["data"]["name"] == "Alert"

	def test_llama_chat_template_tokens_stripped(self):
		text = (
			'<|start_header_id|>assistant<|end_header_id|>\n'
			'[{"op": "create", "doctype": "Notification", "data": {"name": "X"}}]\n'
			'<|eot_id|>'
		)
		result = _extract_changes(text)
		assert len(result) == 1

	def test_prose_then_single_json_block(self):
		"""The common case: agent explains, then emits one clean JSON array."""
		text = (
			"I analyzed the requirements and determined we need one Notification.\n\n"
			'[{"op": "create", "doctype": "Notification", "data": {"name": "N1"}}]'
		)
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Notification"

	def test_two_different_blocks_picks_first(self):
		"""If the agent emits two DIFFERENT arrays, we pick the first one
		intentionally - the agent's first attempt is usually the cleaner one
		before it starts second-guessing itself."""
		text = (
			'[{"op": "create", "doctype": "A", "data": {"name": "first"}}]\n\n'
			'Actually let me reconsider:\n\n'
			'[{"op": "create", "doctype": "B", "data": {"name": "second"}}]'
		)
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "A"
		assert result[0]["data"]["name"] == "first"

	def test_repeated_plan_object_wrapper(self):
		"""Same repetition scenario but inside {plan: [...]} wrapper."""
		text = (
			'```json\n'
			'{"plan": [{"op": "create", "doctype": "Notification", "data": {"name": "X"}}]}\n'
			'```\n\n'
			'Let me try again:\n\n'
			'```json\n'
			'{"plan": [{"op": "create", "doctype": "Notification", "data": {"name": "X"}}]}\n'
			'```'
		)
		result = _extract_changes(text)
		assert len(result) == 1
		assert result[0]["doctype"] == "Notification"


class TestValidateChangesetShape:
	"""Contract validation for changeset items before the rescue/preview step."""

	def test_valid_item_returns_no_errors(self):
		items = [
			{"op": "create", "doctype": "Notification", "data": {"doctype": "Notification", "name": "Test"}},
		]
		assert _validate_changeset_shape(items) == []

	def test_invalid_op_flagged(self):
		items = [{"op": "magic", "doctype": "Notification", "data": {"doctype": "Notification"}}]
		errors = _validate_changeset_shape(items)
		assert any("op=" in e for e in errors)

	def test_missing_doctype_flagged(self):
		items = [{"op": "create", "doctype": "", "data": {}}]
		errors = _validate_changeset_shape(items)
		assert any("doctype=" in e for e in errors)

	def test_non_dict_data_flagged(self):
		items = [{"op": "create", "doctype": "Notification", "data": "oops"}]
		errors = _validate_changeset_shape(items)
		assert any("data=" in e for e in errors)

	def test_outer_inner_doctype_mismatch_flagged(self):
		items = [{
			"op": "create", "doctype": "Notification",
			"data": {"doctype": "Server Script", "name": "X"},
		}]
		errors = _validate_changeset_shape(items)
		assert any("does not match" in e for e in errors)

	def test_non_dict_item_flagged(self):
		items = ["not a dict", {"op": "create", "doctype": "Notification", "data": {}}]
		errors = _validate_changeset_shape(items)
		assert any("expected dict" in e for e in errors)

	def test_multiple_items_all_valid(self):
		items = [
			{"op": "create", "doctype": "Notification", "data": {"doctype": "Notification"}},
			{"op": "create", "doctype": "Server Script", "data": {"doctype": "Server Script"}},
			{"op": "update", "doctype": "Custom Field", "data": {"doctype": "Custom Field"}},
		]
		assert _validate_changeset_shape(items) == []


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
