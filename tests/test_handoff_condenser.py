"""Tests for the Phase 2 handoff-summary condenser.

Covers:
  - compact JSON passthrough (no change)
  - prose-wrapped JSON extraction
  - markdown code fence stripping
  - balanced-brace extraction when prose surrounds JSON
  - strings containing `{` or `]` do not fool the extractor
  - tail truncation fallback when no JSON is present
  - generate_changeset is skipped (its raw must survive unchanged)
  - callback preserves the original on internal error
"""

import json

import pytest

from alfred.agents.condenser import (
	_find_outermost_json,
	condense_raw_output,
	make_condenser_callback,
	_MAX_FALLBACK_CHARS,
)


class _FakeTaskOutput:
	def __init__(self, raw):
		self.raw = raw


class TestCondenseRawOutput:
	def test_empty_string_passes_through(self):
		assert condense_raw_output("gather_requirements", "") == ""

	def test_none_returns_empty(self):
		assert condense_raw_output("gather_requirements", None) == ""

	def test_plain_json_object_compacted(self):
		raw = '{\n  "summary": "Build a thing",\n  "customizations_needed": []\n}'
		out = condense_raw_output("gather_requirements", raw)
		assert len(out) < len(raw)
		assert json.loads(out) == {"summary": "Build a thing", "customizations_needed": []}

	def test_plain_json_array_compacted(self):
		raw = '[\n  {"a": 1},\n  {"b": 2}\n]'
		out = condense_raw_output("design_solution", raw)
		assert json.loads(out) == [{"a": 1}, {"b": 2}]
		assert "\n" not in out

	def test_markdown_fence_json_stripped(self):
		raw = '```json\n{"recommendation": "proceed"}\n```'
		out = condense_raw_output("assess_feasibility", raw)
		assert json.loads(out) == {"recommendation": "proceed"}

	def test_markdown_fence_no_language(self):
		raw = '```\n{"x": 1}\n```'
		out = condense_raw_output("assess_feasibility", raw)
		assert json.loads(out) == {"x": 1}

	def test_prose_wrapped_json_extracted(self):
		raw = (
			"Here is my analysis. I reviewed the doctypes and found the following.\n"
			'{"risk_level": "low", "recommendation": "proceed"}\n'
			"Let me know if you need anything else."
		)
		out = condense_raw_output("assess_feasibility", raw)
		assert json.loads(out) == {"risk_level": "low", "recommendation": "proceed"}
		assert len(out) < len(raw)

	def test_string_with_brace_does_not_fool_extractor(self):
		"""A quoted string containing `}` must not prematurely close the object."""
		raw = 'prose {"msg": "hello } world", "ok": true} trailing'
		out = condense_raw_output("gather_requirements", raw)
		assert json.loads(out) == {"msg": "hello } world", "ok": True}

	def test_escaped_quote_in_string(self):
		raw = '{"path": "C:\\\\foo\\\\bar", "ok": true}'
		out = condense_raw_output("gather_requirements", raw)
		assert json.loads(out)["ok"] is True

	def test_pure_prose_tail_truncated(self):
		raw = "x" * (_MAX_FALLBACK_CHARS * 3)
		out = condense_raw_output("gather_requirements", raw)
		assert len(out) < len(raw)
		assert out.startswith("... (truncated) ...")

	def test_short_prose_passes_through(self):
		raw = "Short summary with no JSON at all."
		out = condense_raw_output("gather_requirements", raw)
		assert out == raw

	def test_invalid_json_substring_falls_through(self):
		raw = "prose {not valid json here} more prose"
		out = condense_raw_output("gather_requirements", raw)
		# No valid JSON, short prose: returned untouched
		assert out == raw

	def test_nested_json_preserved(self):
		raw = (
			'{"doctypes": [{"name": "Book", "fields": [{"fieldname": "title"}]}], '
			'"workflows": []}'
		)
		out = condense_raw_output("design_solution", raw)
		parsed = json.loads(out)
		assert parsed["doctypes"][0]["fields"][0]["fieldname"] == "title"


class TestFindOutermostJson:
	def test_no_json_returns_none(self):
		assert _find_outermost_json("just prose") is None

	def test_object_found(self):
		assert _find_outermost_json('prose {"a": 1} more') == '{"a": 1}'

	def test_array_found(self):
		assert _find_outermost_json("text [1, 2, 3] end") == "[1, 2, 3]"

	def test_picks_whichever_opens_first(self):
		"""If both `[` and `{` appear, the earlier one wins."""
		assert _find_outermost_json('text [{"a": 1}] end') == '[{"a": 1}]'

	def test_nested_balanced_counting(self):
		text = '{"outer": {"inner": {"deep": 1}}}'
		assert _find_outermost_json(text) == text

	def test_unbalanced_returns_none(self):
		assert _find_outermost_json('{"a": 1') is None


class TestCondenserCallback:
	def test_skipped_tasks_return_none(self):
		"""generate_changeset must not be condensed - it's the final artifact."""
		assert make_condenser_callback("generate_changeset") is None
		assert make_condenser_callback("validate_changeset") is None
		assert make_condenser_callback("deploy_changeset") is None

	def test_upstream_tasks_get_a_callback(self):
		for name in ("gather_requirements", "assess_feasibility", "design_solution"):
			cb = make_condenser_callback(name)
			assert cb is not None, f"Expected callback for {name}"

	def test_callback_mutates_raw_in_place(self):
		cb = make_condenser_callback("gather_requirements")
		output = _FakeTaskOutput(
			raw='prose {\n  "summary": "x",\n  "items": [1, 2, 3]\n} trailing text'
		)
		original_len = len(output.raw)
		cb(output)
		assert len(output.raw) < original_len
		assert json.loads(output.raw)["summary"] == "x"

	def test_callback_preserves_when_condensed_not_smaller(self):
		cb = make_condenser_callback("gather_requirements")
		raw = '{"x":1}'  # already compact
		output = _FakeTaskOutput(raw=raw)
		cb(output)
		# Compact JSON round-trips to the same (or shorter) string; either way
		# we shouldn't see a longer version than the original.
		assert len(output.raw) <= len(raw)

	def test_callback_preserves_on_exception(self):
		"""If the output object is weird, the callback swallows and keeps raw."""
		cb = make_condenser_callback("gather_requirements")

		class BadOutput:
			# raw is a property that raises on read
			@property
			def raw(self):
				raise RuntimeError("boom")

		bad = BadOutput()
		# Should not raise
		cb(bad)

	def test_callback_handles_missing_raw_attribute(self):
		cb = make_condenser_callback("gather_requirements")
		class NoRaw:
			pass
		# Should not raise
		cb(NoRaw())

	def test_callback_handles_non_string_raw(self):
		cb = make_condenser_callback("gather_requirements")
		output = _FakeTaskOutput(raw=12345)
		cb(output)
		# Non-string raw: left alone (early return)
		assert output.raw == 12345
