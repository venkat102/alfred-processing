# Alfred Processing App — Docker Image
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Copy dependency file first for Docker layer caching
COPY pyproject.toml README.md ./

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Copy application code
COPY alfred/ ./alfred/

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash alfreduser && \
    chown -R alfreduser:alfreduser /app
USER alfreduser

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

# Run with uvicorn
ENV WORKERS=2
ENV HOST=0.0.0.0
ENV PORT=8000

CMD uvicorn alfred.main:app \
    --host ${HOST} \
    --port ${PORT} \
    --workers ${WORKERS} \
    --timeout-keep-alive 65 \
    --log-level info
