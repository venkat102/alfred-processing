"""Pipeline-lifecycle tracking for graceful shutdown.

Implements the actual wire behind ``alfred/main.py``'s TD-M6 hooks.
The lifespan handler at startup sets:

  app.state.shutting_down = False
  app.state.active_pipelines = 0

…and on shutdown polls ``active_pipelines > 0`` for up to
``GRACEFUL_SHUTDOWN_TIMEOUT`` seconds. The wire was missing — the
counter was initialised but never moved by anything in the request
path. ``track_pipeline`` is what every entry point now wraps so the
poll loop has something real to wait on.

Usage::

    from alfred.api.lifecycle import is_shutting_down, track_pipeline

    if is_shutting_down(app.state):
        raise HTTPException(503, "Shutting down")
    async with track_pipeline(app.state):
        await AgentPipeline(ctx).run()

The counter lives on ``app.state`` (FastAPI's per-application namespace)
so it's visible to the lifespan handler without globals. Single-process
only — multi-worker deploys still need TD-H7 sticky routing because
each worker has its own ``app.state``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

logger = logging.getLogger("alfred.lifecycle")


def is_shutting_down(app_state: Any) -> bool:
	"""Return True if the lifespan has flipped to shutdown mode.

	Defaults to False if the attribute is missing — covers tests that
	construct an app without driving the lifespan handler. The default
	is "we are still up" so a misconfigured test can't accidentally
	short-circuit every pipeline call into a 503.
	"""
	return bool(getattr(app_state, "shutting_down", False))


@asynccontextmanager
async def track_pipeline(app_state: Any):
	"""Increment ``active_pipelines`` for the duration of a pipeline run.

	Decrement runs in ``finally`` so a crash, timeout, or cancellation
	releases the counter. Clamped at zero on the way down so a wire
	bug elsewhere can't push us into the negatives and confuse the
	shutdown poll.

	The increment uses ``getattr(..., 0)`` so if the lifespan handler
	hasn't set the attribute yet (test path), we still create it
	rather than erroring.
	"""
	app_state.active_pipelines = getattr(app_state, "active_pipelines", 0) + 1
	try:
		yield
	finally:
		# Clamp at zero — defends against double-decrement if a wire
		# bug ever wraps the same coroutine twice.
		current = getattr(app_state, "active_pipelines", 1)
		app_state.active_pipelines = max(0, current - 1)
