"""Redact sensitive values from log records before they hit stdout.

Rationale: the hot path logs site_config (which may include the client's
``llm_api_key``), JWT tokens in debug traces, and similar bearer-token
strings. Leaking these into stdout puts them in CloudWatch / Loki / any
log aggregator indefinitely. This formatter scans structured log args
(dicts, lists, tuples) for known-sensitive keys and replaces their values
with ``***REDACTED***``, plus regex-sweeps the formatted message for
Bearer-token shapes that may have been string-interpolated.

What this DOES redact:
  - ``logger.info("handshake: %s", {"api_key": "x", "llm_api_key": "y"})``
  - Nested dicts / lists of dicts.
  - ``Authorization: Bearer <token>`` substrings in any message.

What this does NOT redact (by design):
  - Free-form prompt strings. Prompts are the primary debugging signal;
    over-redaction makes support tickets unworkable.
  - Values stored on the log record as ``extra={...}`` — extras become
    attributes on the record, not args. Adding coverage for extras is a
    separate concern; callers should prefer args for structured data.
  - Exception tracebacks (``exc_info=True``). Tracebacks may contain
    locals; Python's default formatter shows short reprs which usually
    don't include secret values, but this is not guaranteed.

Threat model: a developer reading production logs. This is not a
defence against an attacker with access to the logging pipeline
itself - that attacker would have other paths.
"""

from __future__ import annotations

import logging
import re

# Keys (case-insensitive) whose values must never hit stdout. Add
# sparingly - every addition is a support-debugging blind spot.
_SENSITIVE_KEYS: frozenset[str] = frozenset({
	"api_key",
	"api_secret_key",
	"llm_api_key",
	"llm_api_secret",
	"jwt_token",
	"token",
	"access_token",
	"refresh_token",
	"password",
	"passwd",
	"secret",
	"authorization",
	"bearer",
	"service_api_key",
	"admin_service_key",
})

# Regex sweeps applied to the fully-formatted message line. Catches cases
# where a caller f-string'd a secret into the message instead of passing
# it as a dict arg. Min-length thresholds chosen to avoid false positives
# on log text like "Bearer sensitive data" — real opaque tokens are long.
_MESSAGE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
	# Authorization: Bearer <token>. Require 20+ chars on the token side;
	# any shorter match is almost certainly a word, not a secret.
	(
		re.compile(r"(Bearer\s+)([A-Za-z0-9_\-\.=]{20,})", re.IGNORECASE),
		r"\1***REDACTED***",
	),
	# JWT-shaped triple: <header>.<payload>.<sig> (10+ char segments each).
	# "eyJ" prefix is base64 for "{"" - standard JWT header start.
	(
		re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
		"***REDACTED_JWT***",
	),
)

_REDACTED = "***REDACTED***"


def _redact_value(v: object) -> object:
	"""Redact sensitive values recursively. Returns a new structure; does
	not mutate ``v`` in place so the caller's data is untouched.
	"""
	if isinstance(v, dict):
		return _redact_dict(v)
	if isinstance(v, list):
		return [_redact_value(item) for item in v]
	if isinstance(v, tuple):
		return tuple(_redact_value(item) for item in v)
	return v


def _redact_dict(d: dict) -> dict:
	out: dict = {}
	for k, v in d.items():
		if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
			# Preserve type hint (empty string stays empty; non-empty
			# becomes the redacted marker). Avoids a downstream formatter
			# crashing on an unexpected None.
			out[k] = _REDACTED if v not in ("", None) else v
		else:
			out[k] = _redact_value(v)
	return out


def _apply_message_patterns(message: str) -> str:
	for pattern, repl in _MESSAGE_PATTERNS:
		message = pattern.sub(repl, message)
	return message


class RedactingFormatter(logging.Formatter):
	"""logging.Formatter that strips sensitive values before emission.

	Two-stage:
	  1. Walk record.args (and record.msg if it's a dict) and replace
	     sensitive dict values with ***REDACTED***.
	  2. After the standard formatter produces the final string, apply
	     regex patterns to catch secrets that were f-string'd directly
	     into the message.
	"""

	def format(self, record: logging.LogRecord) -> str:
		# Stage 1: redact structured args. We replace the attribute rather
		# than mutating in place so the caller's dict is untouched; the
		# stdlib formatter reads the new attribute when it computes
		# record.getMessage().
		if isinstance(record.msg, dict):
			record.msg = _redact_dict(record.msg)
		if record.args:
			if isinstance(record.args, dict):
				record.args = _redact_dict(record.args)
			elif isinstance(record.args, tuple):
				record.args = tuple(_redact_value(a) for a in record.args)

		# Stage 2: let the base formatter do its thing, then sweep the
		# result for Bearer tokens / JWTs that slipped through stage 1.
		formatted = super().format(record)
		return _apply_message_patterns(formatted)
