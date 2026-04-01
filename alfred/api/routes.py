"""REST API endpoints for the Alfred Processing App.

All endpoints except /health require API key authentication.
Site isolation: site_id is extracted from JWT on WebSocket connections.
For REST endpoints, site_id comes from the request body (validated via API key).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from alfred import __version__
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

# Server-side rate limit defaults — NOT overridable by client
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
		except Exception:
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
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	site_id = body.site_config.get("site_id", "unknown")
	user = body.user_context.get("user", "unknown")

	# Rate limit uses SERVER-SIDE default — never trust client-supplied limits
	allowed, remaining, retry_after = await check_rate_limit(
		request.app.state.redis, site_id, user, SERVER_DEFAULT_RATE_LIMIT
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
