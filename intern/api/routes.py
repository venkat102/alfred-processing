"""REST API endpoints for the Alfred Processing App.

All endpoints except /health require API key authentication.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from intern import __version__
from intern.middleware.auth import verify_api_key
from intern.middleware.rate_limit import check_rate_limit
from intern.models.messages import (
	ErrorResponse,
	TaskCreateRequest,
	TaskCreateResponse,
	TaskMessageResponse,
	TaskStatusResponse,
)
from intern.state.store import StateStore

router = APIRouter()


def _get_store(request: Request) -> StateStore | None:
	"""Get the StateStore from app state, or None if Redis is unavailable."""
	redis = getattr(request.app.state, "redis", None)
	if redis is None:
		return None
	return StateStore(redis)


# ── Health (no auth) ─────────────────────────────────────────────

@router.get("/health")
async def health_check(request: Request):
	"""Health check endpoint. Returns service status and Redis connectivity."""
	redis_status = "disconnected"
	if request.app.state.redis:
		try:
			await request.app.state.redis.ping()
			redis_status = "connected"
		except Exception:
			redis_status = "error"

	return {
		"status": "ok",
		"version": __version__,
		"redis": redis_status,
	}


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
	"""Submit a new task for agent processing.

	Requires API key authentication. Creates a task entry in Redis
	and returns a task_id for tracking.
	"""
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	# Extract site_id and user from the request context
	site_id = body.site_config.get("site_id", "unknown")
	user = body.user_context.get("user", "unknown")

	# Rate limit check
	max_per_hour = body.site_config.get("max_tasks_per_user_per_hour", 20)
	allowed, remaining, retry_after = await check_rate_limit(
		request.app.state.redis, site_id, user, max_per_hour
	)
	if not allowed:
		raise HTTPException(
			status_code=429,
			detail={"error": f"Rate limit exceeded. Retry after {retry_after} seconds.", "code": "RATE_LIMIT"},
			headers={"Retry-After": str(retry_after)},
		)

	# Create the task
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

	Searches across all site namespaces (in production, site_id would
	be extracted from the API key mapping).
	"""
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	# Try to find the task — in a real implementation, site_id would be
	# derived from the API key. For now, we check the task's stored site_id.
	# We use a convention: tasks also store themselves under a global lookup key.
	site_id = request.query_params.get("site_id", "unknown")
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
	"""Get message history for a task from the event stream.

	Use since_id parameter to paginate (get messages after a given ID).
	"""
	store = _get_store(request)
	if store is None:
		raise HTTPException(
			status_code=503,
			detail={"error": "Service unavailable: Redis not connected", "code": "REDIS_UNAVAILABLE"},
		)

	site_id = request.query_params.get("site_id", "unknown")
	events = await store.get_events(site_id, task_id, since_id=since_id)
	return [TaskMessageResponse(id=e["id"], data=e["data"]) for e in events]
