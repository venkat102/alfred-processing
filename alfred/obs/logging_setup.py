"""Structured logging (TD-M3).

Configures structlog as the formatter for the stdlib ``logging`` module so
existing ``logging.getLogger(...).info(...)`` calls in the codebase gain
structured output without being rewritten. The configuration is split into
two renderers:

  - ``ConsoleRenderer`` when ``LOG_FORMAT=console`` (dev default) —
    human-readable lines with ANSI colour.
  - ``JSONRenderer`` when ``LOG_FORMAT=json`` (production) — one JSON
    object per line, optimised for Loki / CloudWatch indexing.

Context propagation: ``structlog.contextvars.bind_contextvars(...)`` is
used by the WebSocket auth handler and any HTTP middleware to pin
``site_id`` / ``user`` / ``conversation_id`` for the lifetime of a
request; every log line produced inside that context automatically
carries those fields. Callers unbind at the end of the request via
``clear_contextvars()`` (or rely on a fresh contextvar frame per
asyncio task).

Redaction: the existing ``_redact_dict`` / ``_apply_message_patterns``
logic from ``alfred.obs.log_redaction`` is applied as a structlog
processor so both ``logger.info("...", extra={...})`` and
``structlog.get_logger().info("...", key=val)`` paths are scrubbed.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

import structlog

from alfred.obs.log_redaction import (
	_apply_message_patterns,
	_redact_dict,
	_redact_value,
)


def _redact_processor(
	logger: logging.Logger, name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
	"""structlog processor: redact sensitive keys across the event dict.

	Handles top-level keys (``event_dict["api_key"]`` from a
	``logger.info("...", api_key=...)`` call) and nested dicts that a
	caller passed via ``logger.info("...", site_config={...})``.

	Stdlib-bridge callers (``logging.getLogger(...).info("x=%s", {...})``)
	have their ``record.args`` redacted earlier by ``_RedactingFilter``
	— by the time structlog sees the event_dict, the message has
	already been formatted from safe args.
	"""
	return _redact_dict(event_dict)


class _RedactingFilter(logging.Filter):
	"""stdlib logging.Filter that walks LogRecord.args for sensitive keys.

	Runs before the formatter (ProcessorFormatter) applies ``record.
	getMessage()``, which is where %-formatting stringifies args into
	the message. Mutates a fresh copy so the caller's data is left
	untouched.
	"""

	def filter(self, record: logging.LogRecord) -> bool:
		if isinstance(record.msg, dict):
			record.msg = _redact_dict(record.msg)
		if record.args:
			if isinstance(record.args, dict):
				record.args = _redact_dict(record.args)
			elif isinstance(record.args, tuple):
				record.args = tuple(_redact_value(a) for a in record.args)
		return True


def _redact_message_patterns_processor(
	logger: logging.Logger, name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
	"""structlog processor: sweep the ``event`` (message) for Bearer /
	JWT shapes that were string-interpolated instead of passed as args.

	Runs AFTER ``_redact_processor`` — by now all known sensitive keys
	have been stringified into the event where applicable, but the
	regex catch-all is cheap insurance.
	"""
	event = event_dict.get("event")
	if isinstance(event, str):
		event_dict["event"] = _apply_message_patterns(event)
	return event_dict


def configure_logging(log_level: int, log_format: str = "console") -> None:
	"""Install structlog as the formatter for the stdlib root logger.

	Idempotent — calling twice reconfigures in place (useful for tests
	that reset logging state).

	Args:
		log_level: e.g. ``logging.INFO``. Applied to the root logger
			and the ``alfred`` namespace; library loggers stay at
			WARNING (see main.py for the per-library overrides).
		log_format: ``"json"`` for production (one JSON object per
			line) or ``"console"`` for human-readable dev output.
	"""
	# Shared processor chain: bind contextvars, attach timestamp +
	# logger name, then redact. stdlib-bridge callers are scrubbed
	# earlier by the _RedactingFilter on the handler (see below); the
	# processors below handle redaction for native structlog callers
	# and the final message-pattern sweep for everyone.
	shared_processors: list[structlog.types.Processor] = [
		structlog.contextvars.merge_contextvars,
		structlog.processors.add_log_level,
		structlog.processors.TimeStamper(fmt="iso", utc=True),
		_redact_processor,
		_redact_message_patterns_processor,
	]

	# Renderer chosen by env var.
	if log_format == "json":
		renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
	else:
		renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

	# structlog side: top-level ``structlog.get_logger(...)`` callers
	# get the full chain + renderer.
	structlog.configure(
		processors=[
			*shared_processors,
			structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
		],
		logger_factory=structlog.stdlib.LoggerFactory(),
		wrapper_class=structlog.stdlib.BoundLogger,
		cache_logger_on_first_use=True,
	)

	# stdlib side: install a handler whose formatter is a structlog
	# ProcessorFormatter. This lets ``logging.getLogger(...).info(...)``
	# calls flow through the same processor chain and renderer.
	formatter = structlog.stdlib.ProcessorFormatter(
		foreign_pre_chain=shared_processors,
		processors=[
			structlog.stdlib.ProcessorFormatter.remove_processors_meta,
			renderer,
		],
	)
	handler = logging.StreamHandler(stream=sys.stdout)
	handler.setFormatter(formatter)
	# Filter runs BEFORE the formatter, so record.args are redacted
	# before ProcessorFormatter's getMessage() stringifies them into
	# the event.
	handler.addFilter(_RedactingFilter())

	root = logging.root
	root.handlers = [handler]
	root.setLevel(log_level)
	logging.getLogger("alfred").setLevel(log_level)
	# Library chatter — see main.py notes.
	logging.getLogger("websockets").setLevel(logging.WARNING)
	logging.getLogger("httpcore").setLevel(logging.WARNING)
	logging.getLogger("LiteLLM").setLevel(logging.WARNING)


def default_log_format() -> str:
	"""Read the log format from env, with sensible defaults.

	Rule: ``LOG_FORMAT`` wins if explicitly set; otherwise fall back to
	``json`` when ``LOG_LEVEL`` is INFO or higher AND stdout is not a
	TTY (i.e. running under uvicorn/docker), else ``console``.
	"""
	explicit = os.environ.get("LOG_FORMAT")
	if explicit:
		return explicit.lower()
	if not sys.stdout.isatty():
		return "json"
	return "console"


def bind_request_context(
	*,
	site_id: str | None = None,
	user: str | None = None,
	conversation_id: str | None = None,
	**extra: Any,
) -> None:
	"""Bind per-request structured context.

	Call this from the WebSocket auth handler (once the JWT is verified)
	or from an HTTP middleware at the start of request handling.
	Every ``logger.info(...)`` call inside the same asyncio task /
	contextvars frame will automatically carry these fields.

	Skips keys whose value is ``None`` so partial context doesn't
	clutter the output.
	"""
	binding = {
		k: v for k, v in {
			"site_id": site_id,
			"user": user,
			"conversation_id": conversation_id,
			**extra,
		}.items()
		if v is not None
	}
	if binding:
		structlog.contextvars.bind_contextvars(**binding)


def clear_request_context() -> None:
	"""Drop the contextvars frame bound by ``bind_request_context``.

	Call in a ``finally`` block at the end of a request / connection so
	later work on the same asyncio task doesn't inherit stale fields.
	"""
	structlog.contextvars.clear_contextvars()
