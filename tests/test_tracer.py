"""Tests for the Phase 3 #14 pipeline tracer.

Covers:
  - Span lifecycle: start/finish, duration recorded, idempotent finish.
  - attrs / events mutation, None values filtered.
  - Nested spans share trace_id, child.parent_id == parent.span_id.
  - Sibling spans (sequential) reset the ContextVar correctly.
  - Exception path sets status=error and still calls the exporter.
  - JSONL exporter writes one line per finished span.
  - stdout exporter writes to stderr without raising.
  - Disabled tracer is a silent no-op, still yields a span for .set/.event.
  - configure_from_env honors ALFRED_TRACING_ENABLED and trace path env vars.
  - Exporter failures are swallowed and never block the pipeline.
"""

import asyncio
import json
from pathlib import Path

import pytest

from alfred.obs.tracer import (
	Span,
	Tracer,
	configure_from_env,
	jsonl_file_exporter,
	stdout_exporter,
)
from alfred.obs.tracer import (
	tracer as global_tracer,
)


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def fresh_tracer():
	"""Yield a fresh Tracer with no exporters, enabled by default."""
	t = Tracer()
	t.enable()
	yield t
	t.clear_exporters()
	t.disable()


class TestSpanLifecycle:
	def test_finish_sets_duration(self):
		s = Span(name="t", trace_id="a", span_id="b")
		import time
		time.sleep(0.01)
		s.finish()
		assert s.end_ns is not None
		assert s.duration_s is not None
		assert s.duration_s >= 0.005

	def test_finish_is_idempotent(self):
		s = Span(name="t", trace_id="a", span_id="b")
		s.finish()
		first_end = s.end_ns
		s.finish()
		assert s.end_ns == first_end

	def test_finish_with_error(self):
		s = Span(name="t", trace_id="a", span_id="b")
		s.finish(status="error", error="ValueError: bad")
		assert s.status == "error"
		assert s.error == "ValueError: bad"

	def test_set_filters_none(self):
		s = Span(name="t", trace_id="a", span_id="b")
		s.set(keep=1, drop=None, also_keep="x")
		assert "keep" in s.attrs
		assert "drop" not in s.attrs
		assert s.attrs["also_keep"] == "x"

	def test_event_records_timestamp(self):
		s = Span(name="t", trace_id="a", span_id="b")
		s.event("thing_happened", detail="x")
		assert len(s.events) == 1
		assert s.events[0]["name"] == "thing_happened"
		assert "t" in s.events[0]
		assert s.events[0]["detail"] == "x"

	def test_to_dict_contains_core_fields(self):
		s = Span(name="phase1", trace_id="tid", span_id="sid", parent_id="pid")
		s.set(items=3)
		s.finish()
		d = s.to_dict()
		assert d["name"] == "phase1"
		assert d["trace_id"] == "tid"
		assert d["span_id"] == "sid"
		assert d["parent_id"] == "pid"
		assert d["attrs"]["items"] == 3
		assert d["status"] == "ok"
		assert d["duration_s"] is not None


class TestTracerContextManager:
	def test_single_span_exports_on_exit(self, fresh_tracer):
		captured = []
		fresh_tracer.register_exporter(captured.append)

		async def work():
			async with fresh_tracer.span("phase1", foo="bar") as s:
				s.set(items=2)

		_run(work())
		assert len(captured) == 1
		assert captured[0]["name"] == "phase1"
		assert captured[0]["attrs"]["foo"] == "bar"
		assert captured[0]["attrs"]["items"] == 2
		assert captured[0]["status"] == "ok"
		assert captured[0]["duration_s"] is not None

	def test_nested_spans_share_trace_id(self, fresh_tracer):
		captured = []
		fresh_tracer.register_exporter(captured.append)

		async def work():
			async with fresh_tracer.span("outer") as outer:
				async with fresh_tracer.span("inner") as inner:
					assert inner.trace_id == outer.trace_id
					assert inner.parent_id == outer.span_id

		_run(work())
		assert len(captured) == 2
		# inner exports first (LIFO), then outer
		assert captured[0]["name"] == "inner"
		assert captured[1]["name"] == "outer"
		assert captured[0]["trace_id"] == captured[1]["trace_id"]
		assert captured[0]["parent_id"] == captured[1]["span_id"]

	def test_sibling_spans_reset_context(self, fresh_tracer):
		captured = []
		fresh_tracer.register_exporter(captured.append)

		async def work():
			async with fresh_tracer.span("a") as _:
				pass
			async with fresh_tracer.span("b") as b:
				# No parent: context was reset after a finished
				assert b.parent_id is None

		_run(work())
		assert {c["name"] for c in captured} == {"a", "b"}

	def test_exception_marks_span_error_and_still_exports(self, fresh_tracer):
		captured = []
		fresh_tracer.register_exporter(captured.append)

		async def work():
			async with fresh_tracer.span("boom"):
				raise RuntimeError("kaboom")

		with pytest.raises(RuntimeError):
			_run(work())
		assert len(captured) == 1
		assert captured[0]["status"] == "error"
		assert "RuntimeError" in captured[0]["error"]
		assert "kaboom" in captured[0]["error"]

	def test_conversation_id_propagates_to_children(self, fresh_tracer):
		captured = []
		fresh_tracer.register_exporter(captured.append)

		async def work():
			async with fresh_tracer.span("outer", conversation_id="conv-1"):
				async with fresh_tracer.span("inner"):
					pass

		_run(work())
		assert len(captured) == 2
		for c in captured:
			assert c["conversation_id"] == "conv-1"

	def test_disabled_tracer_yields_noop_span(self):
		t = Tracer()
		t.disable()
		called = []
		t.register_exporter(called.append)

		async def work():
			async with t.span("noop") as s:
				s.set(k="v")  # must not raise
				s.event("e")

		_run(work())
		# Disabled: exporter is never called
		assert called == []

	def test_exporter_failure_does_not_break_pipeline(self, fresh_tracer):
		fresh_tracer.register_exporter(lambda _: (_ for _ in ()).throw(RuntimeError("bad")))

		async def work():
			async with fresh_tracer.span("x"):
				pass

		# Should not raise
		_run(work())


class TestJsonlExporter:
	def test_writes_one_line_per_span(self, tmp_path):
		path = str(tmp_path / "trace.jsonl")
		exporter = jsonl_file_exporter(path)
		exporter({"name": "a", "duration_s": 0.1})
		exporter({"name": "b", "duration_s": 0.2})

		lines = Path(path).read_text().strip().splitlines()
		assert len(lines) == 2
		parsed = [json.loads(line) for line in lines]
		assert parsed[0]["name"] == "a"
		assert parsed[1]["name"] == "b"

	def test_creates_parent_directory(self, tmp_path):
		path = str(tmp_path / "nested" / "deeper" / "trace.jsonl")
		exporter = jsonl_file_exporter(path)
		exporter({"name": "a"})
		assert Path(path).exists()

	def test_integrated_with_tracer(self, fresh_tracer, tmp_path):
		path = str(tmp_path / "integrated.jsonl")
		fresh_tracer.register_exporter(jsonl_file_exporter(path))

		async def work():
			async with fresh_tracer.span("phase", items=5) as s:
				s.set(foo="bar")

		_run(work())
		lines = Path(path).read_text().strip().splitlines()
		assert len(lines) == 1
		data = json.loads(lines[0])
		assert data["name"] == "phase"
		assert data["attrs"]["items"] == 5
		assert data["attrs"]["foo"] == "bar"


class TestStdoutExporter:
	def test_writes_to_stderr(self, capsys):
		stdout_exporter({
			"name": "phase",
			"duration_s": 0.123,
			"status": "ok",
			"attrs": {"items": 3},
			"error": None,
		})
		captured = capsys.readouterr()
		assert "phase" in captured.err
		assert "0.12s" in captured.err
		assert "items=3" in captured.err

	def test_error_status_included(self, capsys):
		stdout_exporter({
			"name": "failing",
			"duration_s": 1.0,
			"status": "error",
			"attrs": {},
			"error": "ValueError: bad",
		})
		err = capsys.readouterr().err
		assert "status=error" in err
		assert "ValueError" in err

	def test_missing_duration_handled(self, capsys):
		# Shouldn't raise
		stdout_exporter({"name": "x", "status": "ok", "attrs": {}})
		err = capsys.readouterr().err
		assert "dur=?" in err


class TestConfigureFromEnv:
	def test_disabled_by_default(self, monkeypatch):
		monkeypatch.delenv("ALFRED_TRACING_ENABLED", raising=False)
		configure_from_env()
		assert not global_tracer.enabled

	def test_enables_with_flag(self, monkeypatch, tmp_path):
		monkeypatch.setenv("ALFRED_TRACING_ENABLED", "1")
		monkeypatch.setenv("ALFRED_TRACE_PATH", str(tmp_path / "t.jsonl"))
		configure_from_env()
		assert global_tracer.enabled
		# Reset for other tests
		configure_from_env.__wrapped__ if hasattr(configure_from_env, "__wrapped__") else None
		monkeypatch.delenv("ALFRED_TRACING_ENABLED", raising=False)
		configure_from_env()

	def test_recognizes_true_yes_and_1(self, monkeypatch):
		for val in ("1", "true", "TRUE", "yes", "Yes"):
			monkeypatch.setenv("ALFRED_TRACING_ENABLED", val)
			configure_from_env()
			assert global_tracer.enabled, f"Expected enabled for value {val!r}"
		# Reset
		monkeypatch.delenv("ALFRED_TRACING_ENABLED", raising=False)
		configure_from_env()

	def test_rejects_false_and_empty(self, monkeypatch):
		for val in ("", "0", "false", "no"):
			monkeypatch.setenv("ALFRED_TRACING_ENABLED", val)
			configure_from_env()
			assert not global_tracer.enabled, f"Expected disabled for value {val!r}"

	def test_clears_exporters_on_reconfigure(self, monkeypatch, tmp_path):
		# Register an exporter manually, then reconfigure with disabled
		global_tracer.register_exporter(lambda _: None)
		monkeypatch.delenv("ALFRED_TRACING_ENABLED", raising=False)
		configure_from_env()
		assert global_tracer._exporters == []
