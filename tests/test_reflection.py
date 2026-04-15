"""Tests for the Phase 3 minimality reflection step.

Covers:
  - _parse_indices_strict handles the happy path, empty list, bad JSON,
    out-of-range indices, duplicates, and reason alignment.
  - _describe_item produces the expected one-liner per doctype type.
  - reflect_minimality is a no-op when:
    - feature flag is off
    - changeset is empty / single-item / None
    - original prompt is empty
  - reflect_minimality calls litellm when the flag is on and parses
    the response into (kept, removed).
  - Safety net: if the reviewer flags every item, nothing is removed.
  - Exception path: LLM failure returns the original changeset unchanged.
"""

import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from alfred.agents.reflection import (
	_describe_item,
	_parse_indices_strict,
	reflect_minimality,
)


class TestParseIndicesStrict:
	def test_empty_string(self):
		assert _parse_indices_strict("", 3) == ([], [])

	def test_empty_remove_list(self):
		indices, reasons = _parse_indices_strict('{"remove": [], "reasons": []}', 3)
		assert indices == []
		assert reasons == []

	def test_single_index(self):
		indices, reasons = _parse_indices_strict(
			'{"remove": [1], "reasons": ["audit log not requested"]}', 3
		)
		assert indices == [1]
		assert reasons == ["audit log not requested"]

	def test_multiple_indices(self):
		indices, reasons = _parse_indices_strict(
			'{"remove": [0, 2], "reasons": ["r1", "r2"]}', 4
		)
		assert indices == [0, 2]
		assert reasons == ["r1", "r2"]

	def test_markdown_fence_stripped(self):
		raw = '```json\n{"remove": [1], "reasons": ["x"]}\n```'
		indices, reasons = _parse_indices_strict(raw, 3)
		assert indices == [1]

	def test_prose_wrapped_json(self):
		raw = (
			"Thought: Let me review this.\n\n"
			'{"remove": [2], "reasons": ["duplicate custom field"]}\n\n'
			"Done."
		)
		indices, _ = _parse_indices_strict(raw, 3)
		assert indices == [2]

	def test_out_of_range_indices_filtered(self):
		indices, _ = _parse_indices_strict(
			'{"remove": [0, 5, -1, 2], "reasons": ["a", "b", "c", "d"]}', 3
		)
		# only 0 and 2 are in [0, 3)
		assert indices == [0, 2]

	def test_duplicate_indices_deduped(self):
		indices, _ = _parse_indices_strict(
			'{"remove": [1, 1, 1], "reasons": ["r", "r", "r"]}', 3
		)
		assert indices == [1]

	def test_non_integer_indices_skipped(self):
		indices, _ = _parse_indices_strict(
			'{"remove": [0, "not-a-number", 2], "reasons": ["a", "b", "c"]}', 3
		)
		assert indices == [0, 2]

	def test_malformed_json_returns_empty(self):
		assert _parse_indices_strict("{remove: broken}", 3) == ([], [])

	def test_non_dict_returns_empty(self):
		assert _parse_indices_strict("[0, 1, 2]", 3) == ([], [])

	def test_reasons_padded_when_shorter(self):
		indices, reasons = _parse_indices_strict(
			'{"remove": [0, 1], "reasons": ["just one"]}', 3
		)
		assert indices == [0, 1]
		assert len(reasons) == 2
		assert reasons[0] == "just one"
		assert "required" in reasons[1].lower()

	def test_reasons_truncated_when_longer(self):
		indices, reasons = _parse_indices_strict(
			'{"remove": [0], "reasons": ["a", "b", "c"]}', 3
		)
		assert len(reasons) == 1


class TestDescribeItem:
	def test_notification_includes_doctype_and_event(self):
		item = {
			"op": "create", "doctype": "Notification",
			"data": {"name": "Alert", "document_type": "Sales Order", "event": "Submit"},
		}
		desc = _describe_item(item)
		assert "Notification" in desc
		assert "Alert" in desc
		assert "Sales Order" in desc
		assert "Submit" in desc

	def test_custom_field_includes_dt_and_fieldtype(self):
		item = {
			"op": "create", "doctype": "Custom Field",
			"data": {"fieldname": "priority", "dt": "Task", "fieldtype": "Select"},
		}
		desc = _describe_item(item)
		assert "Custom Field" in desc
		assert "priority" in desc
		assert "Task" in desc
		assert "Select" in desc

	def test_server_script_includes_reference_doctype(self):
		item = {
			"op": "create", "doctype": "Server Script",
			"data": {"name": "validate_dates", "reference_doctype": "Leave Application", "doctype_event": "Before Save"},
		}
		desc = _describe_item(item)
		assert "Server Script" in desc
		assert "validate_dates" in desc
		assert "Leave Application" in desc
		assert "Before Save" in desc

	def test_workflow_includes_document_type(self):
		item = {
			"op": "create", "doctype": "Workflow",
			"data": {"name": "Leave Flow", "document_type": "Leave Application"},
		}
		desc = _describe_item(item)
		assert "Workflow" in desc
		assert "Leave Application" in desc

	def test_non_dict_coerced(self):
		assert "not a dict" in _describe_item("not a dict")

	def test_missing_fields_produce_question_marks(self):
		desc = _describe_item({"op": "create"})
		assert "?" in desc


class TestReflectMinimalityFeatureFlag:
	def test_noop_when_flag_off(self, monkeypatch):
		monkeypatch.delenv("ALFRED_REFLECTION_ENABLED", raising=False)
		changes = [{"doctype": "A"}, {"doctype": "B"}]
		kept, removed = asyncio.get_event_loop().run_until_complete(
			reflect_minimality("user request", changes, {})
		)
		assert kept == changes
		assert removed == []

	def test_noop_when_flag_false(self, monkeypatch):
		monkeypatch.setenv("ALFRED_REFLECTION_ENABLED", "false")
		changes = [{"doctype": "A"}, {"doctype": "B"}]
		kept, removed = asyncio.get_event_loop().run_until_complete(
			reflect_minimality("user request", changes, {})
		)
		assert kept == changes
		assert removed == []


class TestReflectMinimalityEdgeCases:
	def setup_method(self):
		os.environ["ALFRED_REFLECTION_ENABLED"] = "1"

	def teardown_method(self):
		os.environ.pop("ALFRED_REFLECTION_ENABLED", None)

	def _run(self, coro):
		return asyncio.get_event_loop().run_until_complete(coro)

	def test_empty_changeset(self):
		kept, removed = self._run(reflect_minimality("prompt", [], {}))
		assert kept == []
		assert removed == []

	def test_single_item_skipped(self):
		changes = [{"doctype": "Notification", "data": {"name": "X"}}]
		kept, removed = self._run(reflect_minimality("prompt", changes, {}))
		# Reflection never prunes a single-item changeset
		assert kept == changes
		assert removed == []

	def test_empty_prompt(self):
		changes = [{"doctype": "A"}, {"doctype": "B"}]
		kept, removed = self._run(reflect_minimality("", changes, {}))
		assert kept == changes
		assert removed == []


class TestReflectMinimalityWithLLM:
	def setup_method(self):
		os.environ["ALFRED_REFLECTION_ENABLED"] = "1"

	def teardown_method(self):
		os.environ.pop("ALFRED_REFLECTION_ENABLED", None)

	def _run(self, coro):
		return asyncio.get_event_loop().run_until_complete(coro)

	def _make_fake_response(self, content):
		fake = MagicMock()
		fake.choices = [MagicMock(message=MagicMock(content=content))]
		return fake

	def test_prunes_flagged_items(self):
		changes = [
			{"op": "create", "doctype": "Notification", "data": {"name": "Alert"}},
			{"op": "create", "doctype": "DocType", "data": {"name": "AuditLog"}},
			{"op": "create", "doctype": "Server Script", "data": {"name": "LogScript"}},
		]
		response = '{"remove": [1, 2], "reasons": ["audit not asked", "log not asked"]}'
		with patch("litellm.completion", return_value=self._make_fake_response(response)):
			kept, removed = self._run(
				reflect_minimality("Send an email when a leave is approved", changes, {})
			)
		assert len(kept) == 1
		assert kept[0]["doctype"] == "Notification"
		assert len(removed) == 2
		assert removed[0]["index"] == 1
		assert removed[0]["item"]["doctype"] == "DocType"
		assert "audit not asked" in removed[0]["reason"]

	def test_safety_net_when_all_flagged(self):
		changes = [
			{"op": "create", "doctype": "Notification", "data": {"name": "A"}},
			{"op": "create", "doctype": "Notification", "data": {"name": "B"}},
		]
		response = '{"remove": [0, 1], "reasons": ["no", "no"]}'
		with patch("litellm.completion", return_value=self._make_fake_response(response)):
			kept, removed = self._run(
				reflect_minimality("Send notifications", changes, {})
			)
		# Safety net: both flagged -> keep everything
		assert kept == changes
		assert removed == []

	def test_empty_remove_list_passes_through(self):
		changes = [
			{"op": "create", "doctype": "Notification", "data": {"name": "X"}},
			{"op": "create", "doctype": "Notification", "data": {"name": "Y"}},
		]
		response = '{"remove": [], "reasons": []}'
		with patch("litellm.completion", return_value=self._make_fake_response(response)):
			kept, removed = self._run(reflect_minimality("request", changes, {}))
		assert kept == changes
		assert removed == []

	def test_llm_exception_passes_through(self):
		changes = [
			{"op": "create", "doctype": "A", "data": {"name": "a"}},
			{"op": "create", "doctype": "B", "data": {"name": "b"}},
		]
		with patch("litellm.completion", side_effect=RuntimeError("network down")):
			kept, removed = self._run(reflect_minimality("request", changes, {}))
		assert kept == changes
		assert removed == []

	def test_malformed_response_passes_through(self):
		changes = [
			{"op": "create", "doctype": "A", "data": {"name": "a"}},
			{"op": "create", "doctype": "B", "data": {"name": "b"}},
		]
		with patch("litellm.completion", return_value=self._make_fake_response("not json at all")):
			kept, removed = self._run(reflect_minimality("request", changes, {}))
		assert kept == changes
		assert removed == []

	def test_preserves_order(self):
		changes = [
			{"op": "create", "doctype": "A", "data": {"name": "a"}},
			{"op": "create", "doctype": "B", "data": {"name": "b"}},
			{"op": "create", "doctype": "C", "data": {"name": "c"}},
			{"op": "create", "doctype": "D", "data": {"name": "d"}},
		]
		response = '{"remove": [1, 2], "reasons": ["x", "y"]}'
		with patch("litellm.completion", return_value=self._make_fake_response(response)):
			kept, removed = self._run(reflect_minimality("request", changes, {}))
		assert len(kept) == 2
		assert kept[0]["data"]["name"] == "a"
		assert kept[1]["data"]["name"] == "d"
