"""Alfred Processing App - FastAPI entry point.

Headless service that orchestrates AI agents for generating Frappe customizations.
Receives tasks from client apps via WebSocket and REST API.
"""

import logging
import os
from contextlib import asynccontextmanager

# Belt-and-braces: disable CrewAI + OTel telemetry before any import that
# transitively pulls in `crewai`. Docker and .env.example already set these
# (see Dockerfile lines 40-42, .env.example lines 64-66), but a local dev
# .env that omits them would silently phone agent metadata home to CrewAI's
# SaaS endpoint every run. setdefault respects an explicit operator
# override (export CREWAI_DISABLE_TELEMETRY=false to opt back in).
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

from alfred.obs.logging_setup import configure_logging, default_log_format

# Resolve log level from env before Settings is loaded, so import-time
# log lines in other modules respect the level. Default INFO in
# production; DEBUG leaks LLM prompts and site_config (potentially
# including the client's LLM API key) into stdout.
_LOG_LEVEL_NAME = os.environ.get("LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)

# TD-M3: structured logging. ``configure_logging`` installs structlog
# as the stdlib root handler's formatter so existing ``logging.
# getLogger(...).info(...)`` calls automatically gain JSON output +
# contextvars (site_id / user / conversation_id bound per request).
# Redaction is preserved via a structlog processor that re-uses the
# same sensitive-key rules as the old ``RedactingFormatter``. Master's
# basicConfig + per-logger level pins are subsumed by configure_logging
# (it sets levels on the same library namespaces internally).
configure_logging(_LOG_LEVEL, log_format=default_log_format())

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

	# TD-M6: graceful-shutdown state. Handlers check
	# app.state.shutting_down before accepting new work.
	app.state.shutting_down = False
	app.state.active_pipelines = 0
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
	except (aioredis.RedisError, OSError, ValueError) as e:
		# RedisError covers connect/auth/timeout from the Redis client.
		# OSError covers the underlying socket failure that escapes the
		# wrapper on some kernels. ValueError covers a malformed
		# REDIS_URL. App boots in Redis-less degraded mode (rate limit,
		# task state, conversation memory all silently skip).
		logger.warning("Redis unavailable at %s: %s", settings.REDIS_URL, e)
		app.state.redis = None

	app.state.settings = settings

	# Echo module-specialist feature flags so it's obvious at a glance
	# which pipeline paths are active. Off/"0"/absent all render as OFF.
	def _flag(name: str) -> str:
		return "ON" if os.environ.get(name) == "1" else "OFF"

	logger.info(
		"Module-specialist flags: ALFRED_PER_INTENT_BUILDERS=%s "
		"ALFRED_MODULE_SPECIALISTS=%s ALFRED_MULTI_MODULE=%s",
		_flag("ALFRED_PER_INTENT_BUILDERS"),
		_flag("ALFRED_MODULE_SPECIALISTS"),
		_flag("ALFRED_MULTI_MODULE"),
	)

	# TD-H7: warn if the operator has scaled uvicorn beyond a single
	# worker. WebSocket-scoped state (ConnectionState, mcp_client
	# pending-response futures, conn._pending_questions) lives in
	# process memory; WORKERS>1 means a load-balancer reconnect onto
	# a different worker silently loses state and orphans the
	# pipeline. Scale by running multiple container replicas instead.
	if settings.WORKERS > 1:
		logger.warning(
			"WORKERS=%d detected. Alfred WebSocket state (ConnectionState "
			"/ pending MCP futures / pending-question callbacks) lives in "
			"each worker's memory and is NOT shared, so a LB reconnect to "
			"a different worker loses session state. Use WORKERS=1 per "
			"container and scale via replicas with sticky WebSocket "
			"routing. See TD-H7 in docs/tech-debt-backlog.md.",
			settings.WORKERS,
		)
		# Same shared-process assumption breaks the REST per-user
		# concurrency cap. _concurrent_tasks lives in rest_runner module
		# memory, so MAX_CONCURRENT_REST_TASKS_PER_USER becomes the cap
		# PER WORKER. With WORKERS=4 a configured cap of 2 silently
		# becomes a global cap of 8 - same TD-H7 root cause, different
		# observable. Operators sizing capacity off the documented value
		# will be off by a factor of WORKERS.
		logger.warning(
			"WORKERS=%d also affects MAX_CONCURRENT_REST_TASKS_PER_USER=%d: "
			"the per-user counter is per-worker, so the effective global "
			"cap is %d. Multiply mentally or pin WORKERS=1 until TD-H7 "
			"moves the counter to Redis.",
			settings.WORKERS,
			settings.MAX_CONCURRENT_REST_TASKS_PER_USER,
			settings.WORKERS * settings.MAX_CONCURRENT_REST_TASKS_PER_USER,
		)

	logger.info("Alfred Processing App ready on %s:%d", settings.HOST, settings.PORT)

	yield

	# ── Graceful shutdown (TD-M6) ──────────────────────────────
	# 1. Flip the flag so WebSocket handlers stop accepting new
	#    prompts. New arrivals get a SHUTTING_DOWN error frame.
	# 2. Poll active_pipelines for up to GRACEFUL_SHUTDOWN_TIMEOUT
	#    seconds. Each in-flight pipeline decrements the counter in
	#    its own `finally`.
	# 3. Close Redis last so tail-end pipeline writes still flush.
	import asyncio as _asyncio

	logger.info("Alfred Processing App shutting down...")
	app.state.shutting_down = True

	deadline = settings.GRACEFUL_SHUTDOWN_TIMEOUT
	waited = 0.0
	poll_interval = 0.5
	while app.state.active_pipelines > 0 and waited < deadline:
		logger.info(
			"Waiting for %d in-flight pipeline(s) to finish "
			"(%.1fs / %ds)...",
			app.state.active_pipelines, waited, deadline,
		)
		await _asyncio.sleep(poll_interval)
		waited += poll_interval
	if app.state.active_pipelines > 0:
		logger.warning(
			"Shutdown deadline reached with %d pipeline(s) still in "
			"flight; they will be cancelled by process exit.",
			app.state.active_pipelines,
		)

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

	settings = get_settings()

	# JWT_SIGNING_KEY — TD-C2. When set, must be a distinct 32+ byte
	# secret; fails fast on misconfig (same-as-API key, or too short).
	# When unset, the WebSocket path falls back to API_SECRET_KEY for
	# backward compatibility; this is logged as a deprecation warning
	# so legacy deployments are visible on every boot.
	jwt_key = settings.JWT_SIGNING_KEY
	if jwt_key:
		if jwt_key == settings.API_SECRET_KEY:
			raise ValueError(
				"JWT_SIGNING_KEY must NOT equal API_SECRET_KEY. The whole "
				"point of splitting the keys (TD-C2) is that a leak of one "
				"cannot compromise the other. Generate a fresh key with: "
				"python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
			)
		if len(jwt_key) < 32:
			raise ValueError(
				f"JWT_SIGNING_KEY is {len(jwt_key)} bytes; must be at least 32 "
				"to resist brute-force against the HMAC. Regenerate with: "
				"python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
			)
	else:
		logger.warning(
			"JWT_SIGNING_KEY is not set - falling back to API_SECRET_KEY for "
			"JWT verification (legacy shared-key mode). A leak of either key "
			"compromises both. Set JWT_SIGNING_KEY to a distinct 32+ byte "
			"secret to enable full key separation. See TD-C2 in "
			"docs/tech-debt-backlog.md for the rollout."
		)

	# CORS — production requires an explicit origin allow-list because
	# combining `*` with allow_credentials=True is invalid per the CORS
	# spec (browsers silently reject credentialed requests). In DEBUG mode
	# we accept `*` as an explicit dev convenience: credentials are
	# disabled to stay spec-compliant, and a loud warning is logged so
	# the config is visible in every startup.
	raw = settings.ALLOWED_ORIGINS.strip()
	dev_wildcard = settings.DEBUG and raw == "*"

	if dev_wildcard:
		logger.warning(
			"CORS: DEBUG=true + ALLOWED_ORIGINS=* - accepting any origin with "
			"credentials DISABLED. This mode is for local development only; "
			"production MUST set DEBUG=false and supply an explicit origin list."
		)
		app.add_middleware(
			CORSMiddleware,
			allow_origins=["*"],
			allow_credentials=False,   # required to make `*` spec-valid
			allow_methods=["*"],
			allow_headers=["*"],
		)
	else:
		if not raw or raw == "*":
			raise ValueError(
				"ALLOWED_ORIGINS must be an explicit comma-separated list of "
				"origins (e.g. http://localhost:8001,https://app.example.com). "
				"`*` and empty are rejected in non-DEBUG mode: combining "
				"either with allow_credentials=True is invalid per the CORS "
				"spec. For local dev, set DEBUG=true to accept `*` (credentials "
				"will be disabled)."
			)
		origins = [o.strip() for o in raw.split(",") if o.strip()]
		if not origins:
			raise ValueError(
				f"ALLOWED_ORIGINS parsed to an empty list: {settings.ALLOWED_ORIGINS!r}"
			)
		app.add_middleware(
			CORSMiddleware,
			allow_origins=origins,
			allow_credentials=True,
			# Explicit methods + headers. The previous `*` was overly
			# permissive; PUT / DELETE / PATCH are not used by any current
			# endpoint and should not be pre-approved for every origin.
			allow_methods=["GET", "POST", "OPTIONS"],
			allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
		)

	# TD-M2: global handler that normalises every HTTPException body
	# into the canonical ErrorResponse shape {error, code, details}.
	# Must be installed before include_router so any HTTPException
	# raised by route handlers flows through it.
	from alfred.api.errors import install_error_handler
	install_error_handler(app)

	# Register routes
	app.include_router(router)
	app.include_router(ws_router)

	# Prometheus /metrics endpoint. Metrics are registered in
	# alfred.obs.metrics and written to from each instrumented module.
	# Using make_asgi_app() keeps the scrape endpoint on its own ASGI
	# mount so there's no FastAPI middleware overhead on the hot path.
	from prometheus_client import make_asgi_app

	# Touching alfred.obs.metrics here ensures the histogram/counter
	# classes are instantiated before any scrape arrives.
	from alfred.obs import metrics  # noqa: F401

	app.mount("/metrics", make_asgi_app())

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
