"""Background pipeline runner for ``POST /api/v1/tasks``.

The WebSocket path (``alfred.api.websocket.connection._run_agent_pipeline``)
is the primary way clients drive the agent pipeline because it gives the
Processing App a duplex back-channel for MCP tool calls into the user's
Frappe site.

REST callers don't have that back-channel. To still let them poll the
``task_id`` flow that ``alfred/models/messages.py::TaskCreateResponse``
documents, this module spawns a background asyncio task that:

  - synthesises a ``ConnectionState``-shaped object so existing pipeline
    phases find the attributes they expect (``site_id``, ``user``,
    ``site_config``, ``send``, ``websocket.app.state.{redis,settings}``);
  - runs the same ``AgentPipeline`` the WebSocket path runs, with
    ``mcp_client=None`` — pipeline phases that need MCP already have
    ``if conn.mcp_client:`` guards and degrade to "best effort";
  - mirrors every emitted message into the Redis event stream so
    ``GET /api/v1/tasks/{id}/messages`` replays a faithful transcript;
  - keeps ``status`` in the task store moving from
    ``queued -> running -> completed | failed`` so
    ``GET /api/v1/tasks/{id}`` reflects real progress.

A REST run produces a degraded experience compared to WebSocket
(no live site introspection), but it is no longer a no-op queue
that nobody drains. Tracked under the audit's C2.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from alfred.api.lifecycle import track_pipeline
from alfred.obs.logging_setup import bind_request_context, clear_request_context
from alfred.obs.tasks import spawn_logged

if TYPE_CHECKING:
	import redis.asyncio as aioredis

	from alfred.config import Settings
	from alfred.models.messages import TaskCreateRequest
	from alfred.state.store import StateStore

logger = logging.getLogger("alfred.rest_runner")


class _RestConn:
	"""Stand-in for ``ConnectionState`` on a REST-driven pipeline run.

	Exposes only the attributes the pipeline phases read off
	``ctx.conn`` (see ``grep ctx\\.conn\\.`` across ``alfred/api/pipeline``
	and ``alfred/api/safety_nets`` — that grep is the contract). New
	pipeline code reading a ``conn`` attribute that's missing here
	will surface as ``AttributeError`` on the first REST request and
	get added; that's the cheapest way to keep the shim honest.

	``send(msg)`` writes into the same Redis stream the WebSocket path
	uses, so ``GET /api/v1/tasks/{id}/messages`` can replay it.
	"""

	def __init__(
		self,
		*,
		site_id: str,
		user: str,
		roles: list[str],
		site_config: dict[str, Any],
		store: StateStore,
		task_id: str,
		redis: aioredis.Redis,
		settings: Settings,
	) -> None:
		self.site_id = site_id
		self.user = user
		self.roles = roles
		self.site_config = site_config
		self.store = store
		self.task_id = task_id
		# Pipeline phases use this as the stream key; for REST the task_id
		# is the conversation. Same id keeps ``GET /messages`` working
		# without a separate concept of "REST run id".
		self.conversation_id = task_id
		# REST has no Frappe back-channel. Pipeline phases already guard
		# `if conn.mcp_client:` before using it, so leaving it as None
		# means MCP-dependent paths skip cleanly instead of crashing.
		self.mcp_client = None
		# These two are read by the WS-path graceful-shutdown logic;
		# the REST runner doesn't reuse that path but the pipeline still
		# touches the attributes during its lifecycle.
		self.active_pipeline = None
		self.active_pipeline_ctx = None
		# WS-only fields the pipeline never touches in REST mode but
		# leaving as benign defaults is cheaper than guarding every read.
		self.last_acked_msg_id: str | None = None
		self.pending_acks: dict[str, dict[str, Any]] = {}

		# Pipeline phases read ``ctx.conn.websocket.app.state.{redis,settings}``
		# directly. Build a minimal duck-typed lookalike — SimpleNamespace
		# keeps it obvious this isn't a real WebSocket so a future reader
		# doesn't try to call .send_json on it.
		self.websocket = SimpleNamespace(
			app=SimpleNamespace(
				state=SimpleNamespace(redis=redis, settings=settings),
			),
		)

	async def send(self, message: dict[str, Any]) -> None:
		"""Mirror an emitted pipeline message into the Redis event stream.

		Failure here must not abort the run — the pipeline can still
		finish and report a final status; we just lose intermediate
		visibility. Worst case the client polls ``GET /tasks/{id}`` and
		sees the final state without the per-step progress trail.
		"""
		if self.store is None:
			return
		try:
			await self.store.push_event(self.site_id, self.conversation_id, message)
		except Exception as e:  # noqa: BLE001 — best-effort stream mirror; the run must continue even if Redis hiccups
			logger.warning(
				"REST runner: stream push failed for task=%s site=%s: %s",
				self.task_id, self.site_id, e,
			)

		# Surface the current agent on its own Redis key (atomic SETEX)
		# so a polling client can show "Architect..." / "Developer..."
		# without tailing the message stream. P1.1: the previous
		# read-modify-write into ``task_state`` raced with the runner's
		# terminal status write — the GET endpoint now overlays this
		# side-channel value on top of the JSON state.
		if message.get("type") == "agent_status":
			agent = (message.get("data") or {}).get("agent")
			if agent:
				try:
					await self.store.set_current_agent(
						self.site_id, self.task_id, agent,
					)
				except Exception as e:  # noqa: BLE001 — current_agent telemetry is non-load-bearing; never block on it
					logger.debug(
						"REST runner: current_agent update failed for task=%s: %s",
						self.task_id, e,
					)


async def _run_rest_task(
	*,
	task_id: str,
	body: TaskCreateRequest,
	redis: aioredis.Redis,
	settings: Settings,
	store: StateStore,
) -> None:
	"""Drive ``AgentPipeline`` for a REST-submitted task.

	Updates the task state on Redis at every lifecycle transition so the
	companion ``GET /api/v1/tasks/{id}`` poll endpoint reflects real
	progress instead of staying stuck on ``queued`` forever (the bug
	this module exists to close).
	"""
	site_id = body.site_config.get("site_id", "unknown")
	user = body.user_context.get("user", "unknown")
	roles = body.user_context.get("roles", [])

	# TD-M3: tag every log line emitted by this background run with
	# the connection identity so REST runs are searchable the same way
	# WS runs are. ``task_id`` doubles as the conversation id here.
	bind_request_context(site_id=site_id, user=user, conversation_id=task_id)
	try:
		# Move the state out of "queued" so a polling client can see we
		# picked it up. ``get_task_state`` returns None if the row was
		# evicted by TTL between POST and the spawn — treat that the same
		# as "no record": skip silently rather than fabricate state.
		state = await store.get_task_state(site_id, task_id)
		if state is None:
			logger.warning(
				"REST runner: task %s missing from store at start; aborting",
				task_id,
			)
			return
		state["status"] = "running"
		await store.set_task_state(site_id, task_id, state)

		conn = _RestConn(
			site_id=site_id, user=user, roles=roles,
			site_config=body.site_config, store=store,
			task_id=task_id, redis=redis, settings=settings,
		)

		# Local import keeps this module's load order independent of the
		# pipeline package — matches the pattern in connection.py.
		from alfred.api.pipeline import AgentPipeline, PipelineContext

		ctx = PipelineContext(
			conn=conn,  # type: ignore[arg-type]
			conversation_id=task_id,
			prompt=body.prompt,
		)

		final_status = "completed"
		error: str | None = None
		# TD-M6: bump app.state.active_pipelines via the same context
		# manager the WS path uses so the lifespan handler waits for
		# the REST run to finish before tearing down Redis. ``app_state``
		# was stitched onto the conn shim at construction time so the
		# wrap doesn't need a second app reference.
		try:
			async with track_pipeline(conn.websocket.app.state):
				await AgentPipeline(ctx).run()
			if ctx.should_stop and ctx.stop_signal is not None:
				final_status = "failed"
				error = ctx.stop_signal.error
		except Exception as e:  # noqa: BLE001 — surface any runtime crash as task=failed instead of letting the spawn die silently
			logger.exception(
				"REST runner: pipeline crashed for task=%s site=%s",
				task_id, site_id,
			)
			final_status = "failed"
			error = f"Pipeline crashed: {e!s}"

		# Final write: refresh from store in case the pipeline updated
		# current_agent / changes / mode mid-run, then layer our final
		# status on top so we don't clobber that progress.
		final_state = await store.get_task_state(site_id, task_id) or state
		final_state["status"] = final_status
		if error:
			final_state["error"] = error
		if ctx.changes:
			final_state["changes"] = ctx.changes
		if ctx.mode:
			final_state["mode"] = ctx.mode
		# Token telemetry from _phase_run_crew. Surfacing it on the
		# REST task_state lets a polling client show "this run cost
		# $0.X / Y tokens" without subscribing to the event stream.
		if ctx.token_tracker is not None:
			final_state["usage"] = ctx.token_tracker.get_summary()
		await store.set_task_state(site_id, task_id, final_state)
	finally:
		clear_request_context()


def schedule_rest_task(
	*,
	task_id: str,
	body: TaskCreateRequest,
	redis: aioredis.Redis,
	settings: Settings,
	store: StateStore,
) -> None:
	"""Spawn ``_run_rest_task`` as a tracked background task.

	Returning to the HTTP caller after this means ``POST /api/v1/tasks``
	stays fast — the long-running pipeline executes off-request. The
	``spawn_logged`` wrapper makes orphan crashes visible in
	``alfred.obs.tasks`` instead of vanishing into asyncio's silent
	exception sink.
	"""
	spawn_logged(
		_run_rest_task(
			task_id=task_id, body=body, redis=redis,
			settings=settings, store=store,
		),
		name=f"rest-task-{task_id}",
	)
