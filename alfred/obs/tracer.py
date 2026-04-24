"""Minimal async-safe pipeline tracer.

Records per-phase spans with durations, arbitrary attributes, events, and
error status. Spans form a parent/child tree via `ContextVar`, so nesting
works across `await` boundaries without passing context explicitly.

Enable via `ALFRED_TRACING_ENABLED=1`. Control the JSONL output location
with `ALFRED_TRACE_PATH` (default `./alfred_trace.jsonl`). Send copies to
stderr with `ALFRED_TRACE_STDOUT=1`.

Why not opentelemetry-api?
  - Zero new dependencies for v1.
  - The span API we need is ~60 lines; an OTel SDK setup would be ~60 lines
    of config anyway.
  - Call sites use `async with tracer.span(name, **attrs)` which is the same
    shape OTel uses, so switching later is mechanical.

Exporter contract: a callable that takes the finished span dict and returns
None. Exporter failures are swallowed - tracing must never block the
pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("alfred.tracer")


def _new_id() -> str:
	"""16 hex chars is plenty to avoid collisions within a single run."""
	return uuid.uuid4().hex[:16]


@dataclass
class Span:
	"""A single traced unit of work. Mutable until `finish()` is called."""

	name: str
	trace_id: str
	span_id: str
	parent_id: str | None = None
	start_ns: float = field(default_factory=time.time)
	end_ns: float | None = None
	duration_s: float | None = None
	attrs: dict[str, Any] = field(default_factory=dict)
	events: list[dict[str, Any]] = field(default_factory=list)
	status: str = "ok"
	error: str | None = None
	conversation_id: str | None = None

	def set(self, **kwargs: Any) -> None:
		"""Attach or overwrite attributes. Last-write-wins."""
		for k, v in kwargs.items():
			if v is None:
				continue
			self.attrs[k] = v

	def event(self, name: str, **attrs: Any) -> None:
		"""Record a timestamped sub-event. Use for 'tool called', 'retry', etc."""
		self.events.append({
			"name": name,
			"t": time.time() - self.start_ns,
			**{k: v for k, v in attrs.items() if v is not None},
		})

	def finish(self, status: str = "ok", error: str | None = None) -> None:
		if self.end_ns is not None:
			return  # idempotent
		self.end_ns = time.time()
		self.duration_s = self.end_ns - self.start_ns
		self.status = status
		if error:
			self.error = error

	def to_dict(self) -> dict[str, Any]:
		return {
			"name": self.name,
			"trace_id": self.trace_id,
			"span_id": self.span_id,
			"parent_id": self.parent_id,
			"start": self.start_ns,
			"end": self.end_ns,
			"duration_s": self.duration_s,
			"status": self.status,
			"error": self.error,
			"attrs": self.attrs,
			"events": self.events,
			"conversation_id": self.conversation_id,
		}


_current_span: ContextVar[Span | None] = ContextVar("alfred_current_span", default=None)


class Tracer:
	"""Global-ish tracer. One instance per process; access via `tracer`."""

	def __init__(self) -> None:
		self._exporters: list[Callable[[dict[str, Any]], None]] = []
		self._enabled: bool = False

	def register_exporter(self, fn: Callable[[dict[str, Any]], None]) -> None:
		self._exporters.append(fn)

	def clear_exporters(self) -> None:
		self._exporters.clear()

	def enable(self) -> None:
		self._enabled = True

	def disable(self) -> None:
		self._enabled = False

	@property
	def enabled(self) -> bool:
		return self._enabled

	def current(self) -> Span | None:
		return _current_span.get()

	@contextlib.asynccontextmanager
	async def span(self, name: str, **attrs: Any):
		"""Create a span and yield it. On exit, finish + export.

		Nests automatically via `ContextVar`. Safe across `await` because
		every task gets its own context copy.
		"""
		if not self._enabled:
			# No-op fast path. Still yield a Span so call sites can .set/.event
			# without branching on the feature flag.
			noop = Span(name=name, trace_id="", span_id="", start_ns=time.time())
			yield noop
			return

		parent = _current_span.get()
		conversation_id = attrs.pop("conversation_id", None) or (
			parent.conversation_id if parent else None
		)
		span = Span(
			name=name,
			trace_id=parent.trace_id if parent else _new_id(),
			span_id=_new_id(),
			parent_id=parent.span_id if parent else None,
			start_ns=time.time(),
			attrs=dict(attrs),
			conversation_id=conversation_id,
		)
		token = _current_span.set(span)
		try:
			yield span
			span.finish(status="ok")
		except Exception as e:
			span.finish(status="error", error=f"{type(e).__name__}: {e}")
			raise
		finally:
			_current_span.reset(token)
			self._export(span)

	def _export(self, span: Span) -> None:
		if not self._exporters:
			return
		data = span.to_dict()
		for fn in self._exporters:
			try:
				fn(data)
			except Exception as e:
				logger.warning("Tracer exporter failed: %s", e)


tracer = Tracer()

_jsonl_lock = threading.Lock()

_DEFAULT_TRACE_PATH = "alfred_trace.jsonl"


def _safe_trace_path(raw: str) -> str:
	"""Validate ALFRED_TRACE_PATH against a permitted-root whitelist.

	The env var is typically set by an operator at deploy time, but any
	attacker who can set process env vars (container injection, CI secret
	leakage, shell history exposure) could otherwise redirect trace writes
	to a sensitive path like /etc/systemd/system or clobber an adjacent
	service's config. This helper resolves the raw input and accepts only
	paths rooted inside:

	  - the current working directory (standard dev case: ``./alfred_trace.jsonl``)
	  - the user's home directory (``~/.alfred/trace.jsonl``)
	  - the system temp dir (``/tmp/trace.jsonl``)

	Anything else falls back to the default filename in CWD, with a
	WARNING logged so the operator notices. Relative ``..`` components
	in the raw input are rejected up front because they're the classic
	traversal signal.
	"""
	if ".." in raw.split(os.sep):
		logger.warning(
			"Rejecting ALFRED_TRACE_PATH=%r - contains '..' traversal component; "
			"falling back to default %r", raw, _DEFAULT_TRACE_PATH,
		)
		return _DEFAULT_TRACE_PATH

	expanded = os.path.expanduser(raw)
	# realpath resolves symlinks too - an attacker can't launder a path
	# through a world-writable symlink that points to /etc/...
	resolved = os.path.realpath(expanded)

	# Build the allowed-root list and resolve each, because on macOS /tmp
	# is a symlink to /private/tmp and tempfile.gettempdir() returns a
	# per-user path under /var/folders - without realpath on both sides,
	# a legitimate /tmp path would false-reject.
	allowed_roots = [
		os.path.realpath(os.getcwd()),
		os.path.realpath(os.path.expanduser("~")),
		os.path.realpath(tempfile.gettempdir()),
		os.path.realpath("/tmp"),
		os.path.realpath("/var/tmp"),
	]
	for root in allowed_roots:
		try:
			# commonpath raises ValueError on unrelated drives (windows); catch.
			if os.path.commonpath([resolved, root]) == root:
				return resolved
		except ValueError:
			continue

	logger.warning(
		"Rejecting ALFRED_TRACE_PATH=%r (resolved=%r) - outside allowed "
		"roots (cwd, $HOME, %s); falling back to default %r",
		raw, resolved, tempfile.gettempdir(), _DEFAULT_TRACE_PATH,
	)
	return _DEFAULT_TRACE_PATH


def jsonl_file_exporter(path: str) -> Callable[[dict[str, Any]], None]:
	"""Return an exporter that appends one JSON object per line to `path`.

	File writes are thread-locked because multiple pipelines may run in
	parallel via the same uvicorn worker pool. O_APPEND would also work
	but relies on kernel-level atomicity of short writes.
	"""
	path = _safe_trace_path(path)
	directory = os.path.dirname(path) or "."
	try:
		os.makedirs(directory, exist_ok=True)
	except OSError:
		pass

	def _export(span: dict[str, Any]) -> None:
		line = json.dumps(span, default=str)
		with _jsonl_lock:
			with open(path, "a", encoding="utf-8") as f:
				f.write(line)
				f.write("\n")

	return _export


def stdout_exporter(span: dict[str, Any]) -> None:
	"""Print a one-line summary of the finished span to stderr.

	Deliberately stderr, not stdout - stdout is reserved for the server's
	main logging. Human-readable form (NOT JSON) so operators can eyeball
	pipelines without piping through `jq`.
	"""
	parts = [
		f"[trace] {span.get('name')}",
		f"dur={span.get('duration_s'):.2f}s" if span.get("duration_s") is not None else "dur=?",
		f"status={span.get('status')}",
	]
	for k, v in (span.get("attrs") or {}).items():
		if isinstance(v, (str, int, float, bool)):
			parts.append(f"{k}={v}")
	if span.get("error"):
		parts.append(f"error={span['error']}")
	sys.stderr.write(" ".join(parts) + "\n")
	sys.stderr.flush()


def configure_from_env() -> None:
	"""Initialize the global tracer from environment variables.

	Safe to call multiple times - clears and re-registers exporters each
	time so test fixtures can reset between runs.
	"""
	tracer.clear_exporters()
	if os.environ.get("ALFRED_TRACING_ENABLED", "").lower() not in {"1", "true", "yes"}:
		tracer.disable()
		return
	tracer.enable()

	path = os.environ.get("ALFRED_TRACE_PATH") or "alfred_trace.jsonl"
	tracer.register_exporter(jsonl_file_exporter(path))

	if os.environ.get("ALFRED_TRACE_STDOUT", "").lower() in {"1", "true", "yes"}:
		tracer.register_exporter(stdout_exporter)

	logger.info(
		"Tracing enabled: jsonl=%s stdout=%s",
		path, "ALFRED_TRACE_STDOUT" in os.environ,
	)


# Initialize once at import time so processes inherit the config without
# every caller having to remember to call configure_from_env.
configure_from_env()
