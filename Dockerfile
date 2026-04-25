# Alfred Processing App — Docker Image
#
# Multi-stage build keeps the runtime image lean: pip cache, build
# toolchain, and intermediate wheels stay in the builder stage.
# Python patch version is pinned so rebuilds are reproducible —
# `3.11-slim` floats over time and a CVE-fix rebuild on Tuesday can
# ship a different base from the one QA'd on Monday.

ARG PYTHON_BASE=python:3.11.9-slim-bookworm


# ── Builder stage ────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS builder

WORKDIR /build

# Install build-time system deps. Kept minimal — runtime image does NOT
# inherit these.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

# Dep-resolve layer — pyproject is what changes least often; leverage
# Docker layer cache. --user installs under /root/.local for easy copy.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir --user .


# ── Runtime stage ────────────────────────────────────────────────
FROM ${PYTHON_BASE} AS runtime

WORKDIR /app

# Non-root user with its own home for pip's --user site.
RUN useradd --create-home --shell /bin/bash alfreduser

# Copy only the installed packages from the builder; no build tools or
# pip cache cross the stage boundary.
COPY --from=builder --chown=alfreduser:alfreduser /root/.local /home/alfreduser/.local
ENV PATH=/home/alfreduser/.local/bin:${PATH}

# App code last so code changes don't invalidate the dep layer.
COPY --chown=alfreduser:alfreduser alfred/ ./alfred/

USER alfreduser

# Exposed port — bind address stays 0.0.0.0 so container networking
# works; prod deployments should put this behind a reverse proxy or LB.
EXPOSE 8000

# Health check — uses the stdlib urllib so we don't need curl in the
# image (curl adds ~4 MB on slim). Reads ${PORT} at runtime so a
# non-default port still works.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, sys, urllib.request; \
sys.exit(0 if urllib.request.urlopen(f'http://localhost:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=3).status == 200 else 1)" \
    || exit 1

# Runtime defaults — override via docker compose env vars.
# TD-H7 Option A: single uvicorn worker per container. WebSocket state
# (ConnectionState, mcp_client._pending_futures, conn._pending_questions)
# lives in process memory and is NOT shared across workers; WORKERS>1
# means a load-balancer reconnect to a different worker loses all
# per-connection state and orphans the pipeline. Scale horizontally via
# replicas + sticky WS routing at the LB, not via threads inside a
# single container. alfred/main.py logs a WARNING on startup if this
# default is overridden.
ENV WORKERS=1
ENV HOST=0.0.0.0
ENV PORT=8000

# CrewAI ships with outbound telemetry to its own SaaS endpoint. Disable
# by default — agents running on customer sites should not phone home.
# All three flags set to cover older and newer CrewAI versions; they
# also short-circuit the OTel SDK init so cold-start is faster.
ENV CREWAI_DISABLE_TELEMETRY=true
ENV CREWAI_DISABLE_TRACKING=true
ENV OTEL_SDK_DISABLED=true

# Exec-form CMD so signals (SIGTERM on shutdown) reach uvicorn directly
# rather than through a shell wrapper — important for graceful-
# shutdown behaviour (TD-M6).
CMD ["sh", "-c", "exec uvicorn alfred.main:app --host ${HOST} --port ${PORT} --workers ${WORKERS} --timeout-keep-alive 65 --log-level info"]
