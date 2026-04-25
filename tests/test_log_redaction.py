"""Tests for alfred.obs.log_redaction.RedactingFormatter."""

from __future__ import annotations

import logging

from alfred.obs.log_redaction import (
	RedactingFormatter,
	_apply_message_patterns,
	_redact_dict,
	_redact_value,
)


def _format(msg, *args) -> str:
	"""Run a single log record through RedactingFormatter and return the output."""
	record = logging.LogRecord(
		name="test",
		level=logging.INFO,
		pathname="test.py",
		lineno=1,
		msg=msg,
		args=args if args else (),
		exc_info=None,
	)
	formatter = RedactingFormatter(fmt="%(message)s")
	return formatter.format(record)


# ── Dict redaction ─────────────────────────────────────────────────


def test_api_key_value_redacted():
	out = _format("cfg: %s", {"api_key": "sk-supersecret-abc", "host": "example"})
	assert "sk-supersecret-abc" not in out
	assert "***REDACTED***" in out
	assert "example" in out  # non-sensitive key preserved


def test_llm_api_key_redacted():
	out = _format("handshake %s", {"llm_api_key": "sk-xyz", "llm_model": "llama3"})
	assert "sk-xyz" not in out
	assert "llama3" in out


def test_jwt_token_key_redacted():
	out = _format("cfg: %s", {"jwt_token": "abc.def.ghi", "user": "admin@x.com"})
	assert "abc.def.ghi" not in out
	assert "admin@x.com" in out


def test_password_key_redacted():
	out = _format("%s", {"password": "hunter2", "user": "navin"})
	assert "hunter2" not in out
	assert "navin" in out


def test_sensitive_key_case_insensitive():
	out = _format("%s", {"API_KEY": "sk-1", "Api_Secret_Key": "sk-2"})
	assert "sk-1" not in out
	assert "sk-2" not in out


def test_nested_dict_redacted():
	out = _format("%s", {"level1": {"level2": {"api_key": "sk-deep"}}})
	assert "sk-deep" not in out
	assert "***REDACTED***" in out


def test_list_of_dicts_redacted():
	out = _format("%s", {"tokens": [{"token": "t1"}, {"token": "t2"}]})
	assert "t1" not in out
	assert "t2" not in out


def test_empty_string_sensitive_value_preserved():
	# Redacting an empty string would be noisy — callers use empty to
	# signal "not set", which is useful debugging info.
	out = _format("%s", {"api_key": ""})
	assert "***REDACTED***" not in out


def test_none_sensitive_value_preserved():
	out = _format("%s", {"api_key": None})
	assert "***REDACTED***" not in out


# ── Prompt / free-text NOT over-redacted ───────────────────────────


def test_prompt_string_not_redacted():
	# Prompts are the primary debugging signal — don't strip them just
	# because they contain sensitive-sounding words.
	prompt = "create a DocType with a password field and an api_key field"
	out = _format("user prompt: %s", prompt)
	assert prompt in out


def test_plain_string_arg_passthrough():
	out = _format("site=%s user=%s", "demo.example.com", "navin@aerele.in")
	assert "demo.example.com" in out
	assert "navin@aerele.in" in out


# ── Message-level regex sweeps ─────────────────────────────────────


def test_bearer_token_in_message_redacted():
	# Real tokens are 20+ chars — this one is 24.
	out = _format("got header: Authorization: Bearer abcdef0123456789XYZabc12")
	assert "abcdef0123456789XYZabc12" not in out
	assert "Bearer ***REDACTED***" in out


def test_bearer_token_in_arg_string_redacted():
	out = _format("header=%s", "Bearer eyJsupersecret-token-value-12345")
	assert "eyJsupersecret-token-value-12345" not in out
	assert "***REDACTED***" in out


def test_bearer_short_word_not_false_positive():
	# Guard against over-redacting log prose. A short word (< 20 chars)
	# after "Bearer" should NOT be treated as a token.
	out = _format("bearer sensitive header from client")
	assert "bearer sensitive" in out
	assert "***REDACTED***" not in out


def test_jwt_triple_in_message_redacted():
	# Full JWT: header.payload.signature all 10+ chars base64url
	jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NSJ9.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
	out = _format("token: %s", jwt)
	assert jwt not in out
	assert "***REDACTED_JWT***" in out


# ── Caller's data not mutated ──────────────────────────────────────


def test_original_dict_not_mutated():
	original = {"api_key": "keep-it", "host": "example"}
	_format("%s", original)
	# Caller still sees its secret — we only redact the log record.
	assert original["api_key"] == "keep-it"
	assert original["host"] == "example"


def test_original_nested_dict_not_mutated():
	original = {"outer": {"api_key": "x"}}
	_format("%s", original)
	assert original["outer"]["api_key"] == "x"


# ── Helpers (unit-level) ───────────────────────────────────────────


def test_redact_dict_drops_sensitive_values():
	out = _redact_dict({"api_key": "x", "ok": 1})
	assert out == {"api_key": "***REDACTED***", "ok": 1}


def test_redact_value_on_scalar_is_identity():
	assert _redact_value(42) == 42
	assert _redact_value("plain") == "plain"
	assert _redact_value(None) is None


def test_apply_message_patterns_bearer():
	out = _apply_message_patterns("x Bearer ABCDEFGH12345678901234567890 y")
	assert "ABCDEFGH12345678901234567890" not in out
	assert "Bearer ***REDACTED***" in out


# ── Edge cases ─────────────────────────────────────────────────────


def test_msg_is_dict_directly():
	# logger.info(data) where data is a dict — .msg IS the dict.
	record = logging.LogRecord(
		name="t", level=logging.INFO, pathname="t.py", lineno=1,
		msg={"api_key": "leak", "ok": 1}, args=(), exc_info=None,
	)
	out = RedactingFormatter(fmt="%(message)s").format(record)
	assert "leak" not in out


def test_dict_args_style_supported():
	# logger.info("%(user)s %(key)s", {"user": "x", "key": "y"})
	record = logging.LogRecord(
		name="t", level=logging.INFO, pathname="t.py", lineno=1,
		msg="%(user)s %(api_key)s",
		args={"user": "navin", "api_key": "leak-me"},
		exc_info=None,
	)
	out = RedactingFormatter(fmt="%(message)s").format(record)
	assert "leak-me" not in out
	assert "navin" in out


def test_format_does_not_raise_on_unusual_types():
	# A bytes value shouldn't cause a formatter crash.
	out = _format("%s", {"api_key": b"bytes-secret", "ok": 1})
	assert "bytes-secret" not in out
