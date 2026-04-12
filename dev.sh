#!/usr/bin/env bash
# Alfred Processing - native development runner
#
# Runs the FastAPI service directly on the host with auto-reload.
# Reuses Frappe's Redis (port 11000) so no separate Redis needed.
# Uses Python 3.11 (crewai's deps don't have wheels for newer Python yet).
#
# Usage:  ./dev.sh
# Stop:   Ctrl+C

set -euo pipefail

cd "$(dirname "$0")"

# ── 1. Ensure venv exists with Python 3.11 ────────────────────────
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
	echo "ERROR: $PYTHON_BIN not found. Install with: brew install python@3.11" >&2
	exit 1
fi

if [ ! -f .venv/bin/python ] || ! .venv/bin/python --version 2>&1 | grep -q "3.11"; then
	echo "→ Creating Python 3.11 venv at .venv/"
	rm -rf .venv
	"$PYTHON_BIN" -m venv .venv
	.venv/bin/pip install --quiet --upgrade pip
	.venv/bin/pip install -e ".[dev]"
	touch .venv/.installed
fi

# Reinstall deps if pyproject.toml has changed since last install
if [ ! -f .venv/.installed ] || [ pyproject.toml -nt .venv/.installed ]; then
	echo "→ pyproject.toml changed, reinstalling deps..."
	.venv/bin/pip install -e ".[dev]"
	touch .venv/.installed
fi

# ── 2. Load .env (gitignored) ─────────────────────────────────────
if [ ! -f .env ]; then
	echo "ERROR: .env not found. Copy .env.example to .env and edit it." >&2
	exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

# ── 3. Sanity-check required vars ─────────────────────────────────
if [ -z "${API_SECRET_KEY:-}" ]; then
	echo "ERROR: API_SECRET_KEY not set in .env" >&2
	exit 1
fi

PORT="${PORT:-8001}"
HOST="${HOST:-0.0.0.0}"

# ── 4. Kill any stale process on PORT ─────────────────────────────
if lsof -ti:"$PORT" >/dev/null 2>&1; then
	echo "→ Killing stale process on port $PORT..."
	lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
	sleep 1
fi

# ── 5. Verify Frappe Redis is reachable (since REDIS_URL points to it) ─
if echo "${REDIS_URL:-}" | grep -q "localhost:11000"; then
	if ! redis-cli -p 11000 ping >/dev/null 2>&1; then
		echo "WARNING: REDIS_URL points to localhost:11000 but Frappe Redis isn't responding." >&2
		echo "         Run 'bench start' in your frappe-bench directory first." >&2
	fi
fi

# ── 6. Run with auto-reload ───────────────────────────────────────
echo "→ Starting Alfred Processing on http://$HOST:$PORT"
echo "  REDIS_URL=$REDIS_URL"
echo "  FALLBACK_LLM_MODEL=${FALLBACK_LLM_MODEL:-<not set>}"
echo "  Press Ctrl+C to stop. Edits to alfred/**/*.py auto-reload."
echo ""

exec .venv/bin/uvicorn alfred.main:app \
	--reload \
	--reload-dir alfred \
	--host "$HOST" \
	--port "$PORT" \
	--log-level info
