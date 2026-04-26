"""Exception-visible asyncio.create_task wrapper.

Bare ``asyncio.create_task(coro)`` swallows exceptions: if the coroutine
raises, the exception surfaces only as a Python "Task exception was
never retrieved" warning at garbage-collection time, which never reaches
the alfred.* loggers. In a long-running server that handles dozens of
pipeline runs per day, this hides real bugs - we were silently losing
traces on background pipeline tasks for weeks before the audit caught it.

``spawn_logged(coro, name=...)`` attaches a done-callback that logs any
exception through a named logger at ERROR level, so failures land in
the same log stream as the rest of the app. Cancellation (expected on
WebSocket disconnect) is logged at DEBUG and is not treated as an
error.

Usage::

    from alfred.obs.tasks import spawn_logged

    task = spawn_logged(some_coro(), name="pipeline-run")

The returned task is a plain ``asyncio.Task`` so callers can still
store, cancel, or await it exactly like ``asyncio.create_task``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

_logger = logging.getLogger("alfred.obs.tasks")


def spawn_logged(
	coro: Coroutine[Any, Any, Any],
	*,
	name: str,
) -> asyncio.Task:
	"""Create a task whose exceptions are logged instead of silently swallowed.

	The ``name`` is included in every log line so it's obvious which
	background task produced the error. Pick names that identify the
	call site (e.g. "pipeline-run", "heartbeat-loop"), not the coroutine
	function name - the function name is often a nested closure.
	"""
	task = asyncio.create_task(coro, name=name)
	task.add_done_callback(_log_task_result)
	return task


def _log_task_result(task: asyncio.Task) -> None:
	"""Done-callback: log ERROR on exception, DEBUG on clean exit / cancel."""
	name = task.get_name()
	if task.cancelled():
		_logger.debug("background task cancelled: %s", name)
		return
	exc = task.exception()
	if exc is None:
		_logger.debug("background task completed: %s", name)
		return
	# asyncio.CancelledError is a BaseException in 3.11+; the task
	# cancelled() check above catches it already, so anything here is a
	# real exception.
	_logger.error(
		"background task %r raised %s: %s",
		name,
		type(exc).__name__,
		exc,
		exc_info=exc,
	)
