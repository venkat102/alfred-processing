"""Alfred Processing App - FastAPI entry point.

Headless service that orchestrates AI agents for generating Frappe customizations.
Receives tasks from client apps via WebSocket and REST API.
"""

import logging
import sys
from contextlib import asynccontextmanager

# Configure application loggers to show in Docker logs
logging.basicConfig(
	level=logging.DEBUG,
	format="%(asctime)s %(name)s %(levelname)s: %(message)s",
	stream=sys.stdout,
)
# Set alfred loggers to DEBUG, reduce noise from libraries
logging.getLogger("alfred").setLevel(logging.DEBUG)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)  # Silence cost calculator spam for Ollama

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from alfred import __version__
from alfred.api.routes import router
from alfred.api.websocket import ws_router
from alfred.config import get_settings

logger = logging.getLogger("alfred.processing")


@asynccontextmanager
async def lifespan(app: FastAPI):
	"""Manage application startup and shutdown lifecycle."""
	settings = get_settings()

	# Startup: initialize Redis connection pool
	logger.info("Alfred Processing App starting up (v%s)", __version__)
	try:
		redis_pool = aioredis.ConnectionPool.from_url(
			settings.REDIS_URL,
			max_connections=settings.REDIS_POOL_SIZE,
			socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
			decode_responses=True,
		)
		redis_client = aioredis.Redis(connection_pool=redis_pool)
		await redis_client.ping()
		app.state.redis = redis_client
		logger.info("Redis connected at %s", settings.REDIS_URL)
	except Exception as e:
		logger.warning("Redis unavailable at %s: %s", settings.REDIS_URL, e)
		app.state.redis = None

	app.state.settings = settings
	logger.info("Alfred Processing App ready on %s:%d", settings.HOST, settings.PORT)

	yield

	# Shutdown: close Redis connection pool
	logger.info("Alfred Processing App shutting down...")
	if app.state.redis:
		await app.state.redis.aclose()
		logger.info("Redis connection closed")
	logger.info("Shutdown complete")


def create_app() -> FastAPI:
	"""Create and configure the FastAPI application."""
	app = FastAPI(
		title="Alfred Processing App",
		description="AI agent orchestration service for Frappe customizations",
		version=__version__,
		lifespan=lifespan,
	)

	# CORS middleware - configurable via ALLOWED_ORIGINS env var
	# Default "*" for development, restrict to specific origins in production
	settings = get_settings()
	origins = settings.ALLOWED_ORIGINS.split(",") if settings.ALLOWED_ORIGINS != "*" else ["*"]
	app.add_middleware(
		CORSMiddleware,
		allow_origins=origins,
		allow_credentials=True,
		allow_methods=["*"],
		allow_headers=["*"],
	)

	# Register routes
	app.include_router(router)
	app.include_router(ws_router)

	return app


app = create_app()

if __name__ == "__main__":
	import uvicorn

	settings = get_settings()
	uvicorn.run(
		"alfred.main:app",
		host=settings.HOST,
		port=settings.PORT,
		workers=settings.WORKERS,
		reload=settings.DEBUG,
		log_level="info",
	)
