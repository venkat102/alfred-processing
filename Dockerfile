# Stage 1: Build dependencies
FROM python:3.11-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir --upgrade pip

# Copy dependency file first for Docker layer caching
COPY pyproject.toml ./

# Install production dependencies
RUN pip install --no-cache-dir --target=/app/deps .

# Stage 2: Runtime image
FROM python:3.11-slim

WORKDIR /app

# Copy installed dependencies from builder
COPY --from=builder /app/deps /usr/local/lib/python3.11/site-packages/

# Copy application code
COPY intern/ ./intern/

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash alfred && \
	chown -R alfred:alfred /app
USER alfred

# Expose API port
EXPOSE 8000

# Health check: verify the API responds
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
	CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Run with uvicorn — worker count configurable via WORKERS env var
ENV WORKERS=4
ENV HOST=0.0.0.0
ENV PORT=8000

CMD uvicorn intern.main:app \
	--host ${HOST} \
	--port ${PORT} \
	--workers ${WORKERS} \
	--timeout-keep-alive 65 \
	--log-level info
