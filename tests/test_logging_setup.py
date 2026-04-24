"""Tests for alfred.obs.logging_setup (TD-M3).

Covers:
  - ``configure_logging`` produces JSON lines when format=json.
  - ``configure_logging`` produces human lines when format=console.
  - ``bind_request_context`` adds site_id / user / conversation_id to
    log output, and ``clear_request_context`` removes them.
  - The ``_RedactingFilter`` scrubs dict args BEFORE the formatter
    stringifies them, so ``logger.info("x=%s", {"api_key": "s"})`` is
    safe.
  - Regex message-sweep still catches Bearer-token / JWT shapes.
  - ``default_log_format`` resolves env → TTY → default.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from alfred.obs.logging_setup import (
	_RedactingFilter,
	bind_request_context,
	clear_request_context,
	configure_logging,
	default_log_format,
)


@contextmanager
def _capture_logs(log_format: str = "json"):
	"""Install configure_logging against an in-memory StringIO so the
	test can read emitted lines. Restores the original root handler
	state on exit so other tests keep their own stdout wiring.

	Yields the StringIO buffer.
	"""
	saved_handlers = list(logging.root.handlers)
	saved_level = logging.root.level
	buf = io.StringIO()
	with patch("alfred.obs.logging_setup.sys.stdout", buf):
		configure_logging(logging.INFO, log_format=log_format)
		try:
			yield buf
		finally:
			logging.root.handlers = saved_handlers
			logging.root.setLevel(saved_level)


def _read_json_lines(buf: io.StringIO) -> list[dict]:
	lines = [ln for ln in buf.getvalue().splitlines() if ln.strip()]
	return [json.loads(ln) for ln in lines]


class TestJsonRenderer:
	def test_emits_one_json_object_per_line(self):
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info("hello world")
		records = _read_json_lines(buf)
		assert len(records) == 1
		assert records[0]["event"] == "hello world"
		assert records[0]["level"] == "info"
		# ISO timestamp with Z suffix (UTC).
		assert records[0]["timestamp"].endswith("Z")

	def test_positional_arg_formatting_works(self):
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info("hi %s", "alice")
		records = _read_json_lines(buf)
		assert records[0]["event"] == "hi alice"

	def test_level_metadata_carries_through(self):
		with _capture_logs("json") as buf:
			log = logging.getLogger("alfred.test")
			log.warning("warn-event")
			log.error("err-event")
		records = _read_json_lines(buf)
		assert {r["event"]: r["level"] for r in records} == {
			"warn-event": "warning",
			"err-event": "error",
		}


class TestConsoleRenderer:
	def test_emits_human_readable_text(self):
		with _capture_logs("console") as buf:
			logging.getLogger("alfred.test").info("readable event")
		output = buf.getvalue()
		# No JSON braces → human format. Message text appears.
		assert "readable event" in output
		# JSON would have {"event":..., "level":...} delimiters.
		assert not output.strip().startswith("{")


class TestContextBinding:
	def setup_method(self):
		clear_request_context()

	def teardown_method(self):
		clear_request_context()

	def test_bind_adds_all_fields_to_log_lines(self):
		with _capture_logs("json") as buf:
			bind_request_context(
				site_id="site-a", user="alice",
				conversation_id="conv-1",
			)
			logging.getLogger("alfred.test").info("after-bind")
		rec = _read_json_lines(buf)[0]
		assert rec["site_id"] == "site-a"
		assert rec["user"] == "alice"
		assert rec["conversation_id"] == "conv-1"

	def test_clear_removes_fields_from_later_lines(self):
		with _capture_logs("json") as buf:
			bind_request_context(site_id="site-a", user="alice")
			clear_request_context()
			logging.getLogger("alfred.test").info("after-clear")
		rec = _read_json_lines(buf)[0]
		assert "site_id" not in rec
		assert "user" not in rec

	def test_bind_skips_none_values(self):
		# Partial context shouldn't leave ``user=None`` in the event.
		with _capture_logs("json") as buf:
			bind_request_context(site_id="site-a", user=None)
			logging.getLogger("alfred.test").info("partial")
		rec = _read_json_lines(buf)[0]
		assert rec["site_id"] == "site-a"
		assert "user" not in rec

	def test_bind_accepts_extra_kwargs(self):
		with _capture_logs("json") as buf:
			bind_request_context(site_id="site-a", request_id="req-123")
			logging.getLogger("alfred.test").info("extra")
		rec = _read_json_lines(buf)[0]
		assert rec["request_id"] == "req-123"


class TestRedactingFilter:
	def test_redacts_sensitive_dict_arg(self):
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info(
				"cfg: %s", {"api_key": "SECRET-VALUE", "other": "ok"},
			)
		rec = _read_json_lines(buf)[0]
		# Secret never appears; placeholder does.
		assert "SECRET-VALUE" not in rec["event"]
		assert "***REDACTED***" in rec["event"]
		# Non-sensitive values preserved.
		assert "ok" in rec["event"]

	def test_redacts_nested_sensitive_dict(self):
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info(
				"handshake: %s", {"site_config": {"llm_api_key": "sk-x"}},
			)
		rec = _read_json_lines(buf)[0]
		assert "sk-x" not in rec["event"]

	def test_regex_sweep_catches_bearer_token_in_message(self):
		# Token interpolated directly into the f-string rather than
		# passed as a dict arg — regex stage must catch it.
		token = "A" * 40
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info(
				f"Authorization: Bearer {token}",
			)
		rec = _read_json_lines(buf)[0]
		assert token not in rec["event"]
		assert "***REDACTED***" in rec["event"]

	def test_regex_sweep_catches_jwt_triple(self):
		jwt = "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYm9iIn0.signature-foo-bar"
		with _capture_logs("json") as buf:
			logging.getLogger("alfred.test").info(f"auth cookie: {jwt}")
		rec = _read_json_lines(buf)[0]
		assert jwt not in rec["event"]

	def test_filter_leaves_non_sensitive_args_untouched(self):
		# LogRecord unwraps a single-dict args tuple into the dict itself,
		# so the filter operates on record.args as a dict (not a tuple);
		# assert the dict content is preserved.
		record = logging.LogRecord(
			name="test",
			level=logging.INFO,
			pathname="test.py",
			lineno=1,
			msg="ok",
			args=({"host": "example.com", "port": 443},),
			exc_info=None,
		)
		_RedactingFilter().filter(record)
		assert record.args == {"host": "example.com", "port": 443}

	def test_filter_returns_true_so_record_is_emitted(self):
		# A Filter returning False would drop the record entirely.
		record = logging.LogRecord(
			name="test", level=logging.INFO, pathname="x.py", lineno=1,
			msg="y", args=(), exc_info=None,
		)
		assert _RedactingFilter().filter(record) is True


class TestDefaultLogFormat:
	def test_explicit_env_wins(self):
		with patch.dict(os.environ, {"LOG_FORMAT": "json"}, clear=False):
			assert default_log_format() == "json"
		with patch.dict(os.environ, {"LOG_FORMAT": "CONSOLE"}, clear=False):
			assert default_log_format() == "console"

	def test_non_tty_defaults_to_json(self):
		with patch.dict(os.environ, {}, clear=False):
			os.environ.pop("LOG_FORMAT", None)
			with patch.object(sys.stdout, "isatty", return_value=False):
				assert default_log_format() == "json"

	def test_tty_defaults_to_console(self):
		with patch.dict(os.environ, {}, clear=False):
			os.environ.pop("LOG_FORMAT", None)
			with patch.object(sys.stdout, "isatty", return_value=True):
				assert default_log_format() == "console"
