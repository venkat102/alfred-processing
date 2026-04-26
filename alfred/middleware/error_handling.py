"""Error handling utilities - retry logic, output validation, graceful degradation.

Provides decorators and utilities for:
- Retry with exponential backoff for transient failures
- Agent output structure validation
- User-friendly error message generation
"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from functools import wraps

logger = logging.getLogger("alfred.errors")

# ── Retry Configuration ──────────────────────────────────────────

DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_MAX_DELAY = 60.0  # seconds
DEFAULT_BACKOFF_FACTOR = 2.0

# Transient error types that should be retried
TRANSIENT_ERRORS = (
	ConnectionError,
	TimeoutError,
	OSError,
	asyncio.TimeoutError,
)


def retry_with_backoff(
	max_retries: int = DEFAULT_MAX_RETRIES,
	base_delay: float = DEFAULT_BASE_DELAY,
	max_delay: float = DEFAULT_MAX_DELAY,
	backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
	retryable_exceptions: tuple = TRANSIENT_ERRORS,
):
	"""Decorator: retry a function with exponential backoff on transient failures."""

	def decorator(func: Callable):
		@wraps(func)
		async def async_wrapper(*args, **kwargs):
			last_exception = None
			for attempt in range(max_retries + 1):
				try:
					return await func(*args, **kwargs)
				except retryable_exceptions as e:
					last_exception = e
					if attempt < max_retries:
						delay = min(base_delay * (backoff_factor ** attempt), max_delay)
						logger.warning(
							"Retry %d/%d for %s: %s (waiting %.1fs)",
							attempt + 1, max_retries, func.__name__, e, delay,
						)
						await asyncio.sleep(delay)
					else:
						logger.error("All %d retries exhausted for %s: %s", max_retries, func.__name__, e)
			raise last_exception

		@wraps(func)
		def sync_wrapper(*args, **kwargs):
			last_exception = None
			for attempt in range(max_retries + 1):
				try:
					return func(*args, **kwargs)
				except retryable_exceptions as e:
					last_exception = e
					if attempt < max_retries:
						delay = min(base_delay * (backoff_factor ** attempt), max_delay)
						logger.warning(
							"Retry %d/%d for %s: %s (waiting %.1fs)",
							attempt + 1, max_retries, func.__name__, e, delay,
						)
						time.sleep(delay)
					else:
						logger.error("All %d retries exhausted for %s: %s", max_retries, func.__name__, e)
			raise last_exception

		if asyncio.iscoroutinefunction(func):
			return async_wrapper
		return sync_wrapper

	return decorator


# ── Agent Output Validation ──────────────────────────────────────

def validate_agent_output(output: str, expected_keys: list[str] | None = None) -> dict:
	"""Validate that an agent's output is valid JSON with expected structure.

	Args:
		output: The raw agent output string.
		expected_keys: Optional list of keys that must be present.

	Returns:
		{"valid": bool, "data": dict | None, "error": str | None}
	"""
	if not output or not output.strip():
		return {"valid": False, "data": None, "error": "Agent produced empty output"}

	# Try to extract JSON from the output (agent may wrap it in markdown)
	json_str = output.strip()

	# Strip markdown code blocks if present
	if json_str.startswith("```"):
		lines = json_str.split("\n")
		# Remove first line (```json) and last line (```)
		lines = [l for l in lines if not l.strip().startswith("```")]
		json_str = "\n".join(lines)

	try:
		data = json.loads(json_str)
	except json.JSONDecodeError:
		# Try to find JSON within the text
		start = json_str.find("{")
		end = json_str.rfind("}") + 1
		if start >= 0 and end > start:
			try:
				data = json.loads(json_str[start:end])
			except json.JSONDecodeError as e:
				return {"valid": False, "data": None, "error": f"Invalid JSON in output: {e}"}
		else:
			return {"valid": False, "data": None, "error": "No JSON found in agent output"}

	if not isinstance(data, dict):
		return {"valid": False, "data": None, "error": f"Expected JSON object, got {type(data).__name__}"}

	# Check expected keys
	if expected_keys:
		missing = [k for k in expected_keys if k not in data]
		if missing:
			return {
				"valid": False,
				"data": data,
				"error": f"Missing required keys: {', '.join(missing)}",
			}

	return {"valid": True, "data": data, "error": None}


# ── User-Friendly Error Messages ─────────────────────────────────

ERROR_MESSAGES = {
	"llm_timeout": "The AI model is taking too long to respond. Please try again in a moment.",
	"llm_unavailable": "The AI service is temporarily unavailable. Please try again later.",
	"redis_unavailable": "Internal state service is unavailable. Your conversation will be saved when it recovers.",
	"permission_denied": "You don't have permission to perform this action. Please contact your administrator.",
	"rate_limited": "You've reached the maximum number of requests. Please wait before trying again.",
	"deployment_failed": "Deployment failed. All changes have been automatically rolled back.",
	"agent_error": "The AI agent encountered an error. The team has been notified.",
	"connection_lost": "Connection to the processing service was lost. Reconnecting...",
	"prompt_blocked": "Your message was flagged by our security filter. Please rephrase your request.",
	"unknown": "An unexpected error occurred. Please try again or contact support.",
}


def get_user_error_message(error_type: str, details: str = "") -> dict:
	"""Get a user-friendly error message for a given error type.

	Returns:
		{"message": str, "type": str, "details": str}
	"""
	message = ERROR_MESSAGES.get(error_type, ERROR_MESSAGES["unknown"])
	return {
		"message": message,
		"type": error_type,
		"details": details,
	}
