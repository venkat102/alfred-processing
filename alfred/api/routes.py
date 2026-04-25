"""REST API endpoints for the Alfred Processing App.

All endpoints except /health require API key authentication.
Site isolation: site_id is extracted from JWT on WebSocket connections.
For REST endpoints, site_id comes from the request body (validated via API key).

URL versioning (TD-M9):
  - /health              unversioned — probe/liveness endpoint, standard practice
  - /api/v1/<resource>   versioned — every functional endpoint
  New endpoints MUST use the /api/v1/ prefix. When breaking changes are
  needed, add /api/v2/* routes and keep /api/v1/* live for a
  deprecation window (Sunset + Deprecation headers on v1).
"""

import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request

from alfred import __version__
from alfred.api.lifecycle import is_shutting_down
from alfred.api.rest_runner import schedule_rest_task
from alfred.middleware.auth import verify_api_key
from alfred.middleware.rate_limit import check_rate_limit
from alfred.models.messages import (
	ErrorResponse,
	TaskCreateRequest,
	TaskCreateResponse,
	TaskMessageResponse,
	TaskStatusResponse,
)
from alfred.state.store import StateStore

router = APIRouter()

# Server-side rate limit defaults - NOT overridable by client
SERVER_DEFAULT_RATE_LIMIT = 20


def _get_store(request: Request) -> StateStore | None:
	redis = getattr(request.app.state, "redis", None)
	if redis is None:
		return None
	return StateStore(redis)


# ── Health (no auth) ─────────────────────────────────────────────

@router.get("/health")
async def health_check(request: Request):
	"""Health check endpoint."""
	redis_status = "disconnected"
	if getattr(request.app.state, "redis", None):
		try:
			await request.app.state.redis.ping()
			redis_status = "connected"
		except (aioredis.RedisError, OSError):
			# Same shape as state.store.is_healthy. Health endpoint
			# never raises; surface "error" to the probe instead.
			redis_status = "error"

	return {"status": "ok", "version": __version__, "redis": redis_status}


# ── Task Management (auth required) ─────────────────────────────

@router.post(
	"/api/v1/tasks",
	response_model=TaskCreateResponse,
	status_code=201,
	responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
)
async def create_task(
	body: TaskCreateRequest,
	request: Request,
	api_key: str = Depends(verify_api_key),
):
	"""Submit a new task for agent processing."""
	# TD-M6 shutdown gate: refuse new tasks once the lifespan has flipped.
	# The WS path returns the same code via an error frame; here we use
	# 503 so a polling client retries with the right backoff.
	if is_shutting_down(request.app.state):
		raise HTTPException(
			status_code=503,
			detail={
				"error": "Service is shutting down for an update. Retry in a moment.",
				"code": "SHUTTING_DOWN",
			},
		)

	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	site_id = body.site_config.get("site_id", "unknown")
	user = body.user_context.get("user", "unknown")

	# Rate limit uses SERVER-SIDE default - never trust client-supplied limits
	allowed, remaining, retry_after = await check_rate_limit(
		request.app.state.redis, site_id, user,
		SERVER_DEFAULT_RATE_LIMIT, source="rest",
	)
	if not allowed:
		raise HTTPException(
			status_code=429,
			detail={"error": f"Rate limit exceeded. Retry after {retry_after} seconds.", "code": "RATE_LIMIT"},
			headers={"Retry-After": str(retry_after)},
		)

	task_id = str(uuid.uuid4())
	task_state = {
		"task_id": task_id,
		"status": "queued",
		"prompt": body.prompt,
		"user": user,
		"site_id": site_id,
		"current_agent": None,
		"site_config": body.site_config,
		"user_context": body.user_context,
	}

	await store.set_task_state(site_id, task_id, task_state)

	# Spawn the pipeline as a background task. POST returns immediately
	# with task_id and the caller polls GET /api/v1/tasks/{id}. Without
	# this dispatch, the row above would sit at status="queued" forever
	# — there is no separate worker process draining the keyspace.
	schedule_rest_task(
		task_id=task_id, body=body,
		redis=request.app.state.redis,
		settings=request.app.state.settings,
		store=store,
	)
	return TaskCreateResponse(task_id=task_id, status="queued")


@router.get(
	"/api/v1/tasks/{task_id}",
	response_model=TaskStatusResponse,
	responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)
async def get_task_status(
	task_id: str,
	request: Request,
	api_key: str = Depends(verify_api_key),
):
	"""Get the current status of a task.

	site_id is required as a query parameter. In production, this would
	be validated against the API key to prevent cross-site access.
	"""
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	site_id = request.query_params.get("site_id", "")
	if not site_id:
		raise HTTPException(status_code=400, detail={"error": "site_id query parameter is required", "code": "MISSING_SITE_ID"})

	state = await store.get_task_state(site_id, task_id)
	if state is None:
		raise HTTPException(
			status_code=404,
			detail={"error": f"Task {task_id} not found", "code": "TASK_NOT_FOUND"},
		)

	return TaskStatusResponse(
		task_id=task_id,
		status=state.get("status", "unknown"),
		current_agent=state.get("current_agent"),
		data=state,
	)


@router.get(
	"/api/v1/tasks/{task_id}/messages",
	response_model=list[TaskMessageResponse],
	responses={401: {"model": ErrorResponse}},
)
async def get_task_messages(
	task_id: str,
	request: Request,
	api_key: str = Depends(verify_api_key),
	since_id: str = "0",
):
	"""Get message history for a task from the event stream."""
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	site_id = request.query_params.get("site_id", "")
	if not site_id:
		raise HTTPException(status_code=400, detail={"error": "site_id query parameter is required", "code": "MISSING_SITE_ID"})

	events = await store.get_events(site_id, task_id, since_id=since_id)
	return [TaskMessageResponse(id=e["id"], data=e["data"]) for e in events]
