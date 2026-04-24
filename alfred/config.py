"""Environment configuration for Alfred Processing App.

All configuration is centralized here via Pydantic Settings. Modules
should prefer ``get_settings().FIELD`` over ``os.environ.get(...)``;
the only exception is ``alfred.security.url_allowlist``, which reads
env directly to stay below the Settings layer and avoid a circular
dependency if Settings itself ever grew SSRF-policy needs.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
	"""Application settings loaded from environment variables."""

	# Server
	HOST: str = "0.0.0.0"
	PORT: int = 8000
	# TD-H7: default 1 because per-connection state lives in worker
	# memory; scaling via uvicorn --workers >1 silently loses WebSocket
	# state on LB reconnect. Scale via container replicas instead.
	# ``main.py`` logs a WARNING on boot if this is overridden higher.
	WORKERS: int = 1
	DEBUG: bool = False

	# Logging
	# INFO in production; DEBUG only for local development. Setting DEBUG
	# globally leaks LLM prompts and site_config (which may include the
	# client's LLM API key) into stdout and drives up log-ingestion cost.
	# Values: DEBUG, INFO, WARNING, ERROR, CRITICAL (case-insensitive).
	LOG_LEVEL: str = "INFO"

	# Security
	# API_SECRET_KEY: bearer token for REST + WebSocket handshake.
	API_SECRET_KEY: str
	# JWT_SIGNING_KEY: HMAC key used to verify WebSocket-handshake JWTs.
	# When unset (empty), the processing app falls back to API_SECRET_KEY
	# for backward-compatibility with pre-TD-C2 deployments — this
	# emits a loud deprecation warning at startup. When set, it MUST
	# be != API_SECRET_KEY and at least 32 bytes; this is enforced by
	# validation in alfred/main.py at boot. Generate with:
	#   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
	# Rollout:
	#   1. JWT issuer (admin portal / client) flips to a new secret.
	#   2. Operator sets JWT_SIGNING_KEY to the same value.
	#   3. Restart — new JWTs verify, old JWTs rejected.
	# Splitting the key means a leak of API_SECRET_KEY (via logs,
	# .env mishandling, memory dump) can no longer be used to forge
	# JWTs.
	JWT_SIGNING_KEY: str = ""

	# JWT iss/aud claims (TD-M1). When both are set:
	#   - create_jwt_token includes them in every issued token
	#   - verify_jwt_token enforces them (PyJWT rejects mismatches)
	# When unset (the default), no enforcement — backward-compatible
	# with pre-TD-M1 tokens. Setting these prevents a token issued for
	# one Alfred instance from being replayed against another if they
	# share the signing key. Typical values:
	#   JWT_ISSUER=admin.example.com
	#   JWT_AUDIENCE=alfred-processing.prod
	JWT_ISSUER: str = ""
	JWT_AUDIENCE: str = ""

	# Redis
	REDIS_URL: str = "redis://redis:6379/0"
	REDIS_POOL_SIZE: int = 20
	REDIS_SOCKET_TIMEOUT: int = 5
	# Task-state TTL in seconds — 7 days default. Every pipeline run
	# writes a task-state blob keyed by conversation_id; without a TTL
	# these accumulate forever and the Redis OOM triggers the maxmemory
	# policy (random eviction in default config), which can evict
	# IN-FLIGHT task state and silently corrupt active pipelines.
	# Override per-call for long-running workflows via
	# TaskStateStore.set_task_state(..., ttl_seconds=...).
	TASK_STATE_TTL_SECONDS: int = 604800

	# Fallback LLM (used when client doesn't provide its own LLM config)
	FALLBACK_LLM_MODEL: str = ""
	FALLBACK_LLM_API_KEY: str = ""
	FALLBACK_LLM_BASE_URL: str = ""

	# LLM call thread pool (TD-H6). Every LLM call blocks a thread for up
	# to 60s because ollama_chat_sync uses stdlib urllib. Running those
	# on FastAPI's default executor (40 threads on Py 3.11) means a
	# burst of concurrent pipelines starves the rest of the app —
	# heartbeat, /health, admin endpoints all hang. Dedicated pool
	# isolates LLM load.
	LLM_POOL_SIZE: int = 16

	# Admin Portal
	ADMIN_PORTAL_URL: str = ""
	ADMIN_SERVICE_KEY: str = ""

	# SSRF allow-list for client-supplied LLM URLs. Comma-separated list
	# of hostnames or CIDR blocks that MAY resolve to private/loopback/
	# link-local IPs. Everything else private is blocked (see
	# alfred/security/url_allowlist.py). DEBUG=true bypasses the private-
	# IP block entirely for local development. Examples:
	#   ALFRED_LLM_ALLOWED_HOSTS=ollama.internal.corp,10.243.88.0/24
	ALFRED_LLM_ALLOWED_HOSTS: str = ""

	# CORS — comma-separated origin allow-list. Must be explicit; `*` is
	# rejected at startup because combining it with allow_credentials=True
	# (which we need for the chat session cookie) is invalid per the CORS
	# spec — browsers reject credentialed requests when the server
	# responds with `*`. Leaving this as `*` was silently broken in
	# browsers but passed tests that don't exercise the credential path.
	# Example: ALLOWED_ORIGINS=http://localhost:8001,https://app.example.com
	ALLOWED_ORIGINS: str = ""

	# WebSocket
	WS_HEARTBEAT_INTERVAL: int = 30

	# Graceful-shutdown window (TD-M6). On SIGTERM we stop accepting
	# new prompts and wait up to this many seconds for in-flight
	# pipelines to finish before the process exits. Kubernetes
	# terminationGracePeriodSeconds on the pod spec should exceed this
	# (default k8s is 30s, so match).
	GRACEFUL_SHUTDOWN_TIMEOUT: int = 30

	# ── ALFRED_* feature flags ──────────────────────────────────
	# Pydantic coerces string env values: "1", "true", "yes", "on"
	# (case-insensitive) → True; everything else → False. This matches
	# the prior ad-hoc `== "1"` / `.lower() in {"1","true","yes"}`
	# idioms the call sites used, so no behavior change at migration.

	# Three-mode chat orchestrator. Off forces mode=dev and skips the
	# orchestrator classify phase. Default matches pre-migration
	# behaviour (the previous os.environ.get() check returned False on
	# unset env var).
	ALFRED_ORCHESTRATOR_ENABLED: bool = False

	# Agent reflection / self-critique pass in the crew pipeline.
	# Default matches pre-migration behaviour (unset → False).
	ALFRED_REFLECTION_ENABLED: bool = False

	# Phase 1 short-circuit (legacy; leave at default unless explicitly
	# disabling Phase 1 for A/B). `True` here means enabled;
	# environment value ALFRED_PHASE1_DISABLED=1 disables it, so
	# semantics are NEGATIVE — see note below.
	ALFRED_PHASE1_DISABLED: bool = False

	# Specialist stack (V1 → V4). Flag stack is nested:
	#   PER_INTENT_BUILDERS   V1 — specialist Builder agents per intent
	#   MODULE_SPECIALISTS    V2 — per-module domain context injection
	#                              (requires V1)
	#   MULTI_MODULE          V3 — primary + secondary module detection
	#                              (requires V2)
	#   REPORT_HANDOFF        V4 — Insights → Report "Save as Report"
	#                              (requires V1)
	ALFRED_PER_INTENT_BUILDERS: bool = False
	ALFRED_MODULE_SPECIALISTS: bool = False
	ALFRED_MULTI_MODULE: bool = False
	ALFRED_REPORT_HANDOFF: bool = False

	# Tracing — writes JSONL spans of every phase for offline analysis.
	# Off by default; turn on for performance investigation.
	ALFRED_TRACING_ENABLED: bool = False
	# Path for the JSONL trace output. Relative or absolute.
	ALFRED_TRACE_PATH: str = "alfred_trace.jsonl"
	# Mirror trace lines to stdout as they're written (noisy; use for
	# local inspection only).
	ALFRED_TRACE_STDOUT: bool = False

	# Frappe Knowledge Base directory override. When set, the knowledge
	# loader reads from this path instead of the bundled default.
	ALFRED_FKB_DIR: str = ""

	model_config = {
		"env_file": ".env",
		"env_file_encoding": "utf-8",
		"case_sensitive": True,
		# Ignore unknown env vars so .env can carry third-party config
		# (CREWAI_*, OTEL_SDK_DISABLED, etc.) without tripping
		# ValidationError(extra_forbidden). ALFRED_* flags are all
		# declared above — adding a new flag means adding it to this
		# model AND a call site, not just setting an env var.
		"extra": "ignore",
	}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
	"""Return the app's Settings, cached for the process lifetime.

	Cached because hot-path code (pipeline phases) read flags per
	request; re-validating the full Pydantic model on every call is
	slow. Tests that monkeypatch env vars after boot MUST call
	``get_settings.cache_clear()`` to see the change.
	"""
	# pydantic-settings fills required fields from the environment
	# (API_SECRET_KEY, REDIS_URL), so the zero-arg call is correct at
	# runtime even though mypy sees them as missing positional args.
	return Settings()  # type: ignore[call-arg]
