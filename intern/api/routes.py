"""REST API endpoints for the Alfred Processing App."""

from fastapi import APIRouter, Request

from intern import __version__

router = APIRouter()


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
