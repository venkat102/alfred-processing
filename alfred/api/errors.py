"""Unified error-response helpers (TD-M2).

Goal: every error body the API emits matches
:class:`alfred.models.messages.ErrorResponse` — ``{error, code, details}``.
Two paths get us there:

1. :func:`raise_error` — callers import this instead of constructing
   ``HTTPException(detail=...)`` by hand. The helper builds an
   ``ErrorResponse``, validates the shape, and raises.

2. :func:`install_error_handler` — global exception handler on the
   FastAPI app. Catches any ``HTTPException`` whose ``detail`` is a
   string (third-party middleware, future oversights) and wraps it
   into the canonical shape with a synthesized code.

Without the global handler, a single ``raise HTTPException(status_code=
400, detail="…")`` slipped into new code would ship a response that
breaks client branching. With it, the response is correct even when
callers forget.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from alfred.models.messages import ErrorResponse


def raise_error(
	status_code: int,
	code: str,
	message: str,
	*,
	details: dict[str, Any] | None = None,
	headers: dict[str, str] | None = None,
) -> None:
	"""Raise an HTTPException with the canonical ErrorResponse shape.

	Args:
		status_code: HTTP status (401, 404, 429, 503, …).
		code: Machine-readable error code (UPPER_SNAKE_CASE). Clients
			branch on this.
		message: Human-readable error message.
		details: Extra structured context (retry_after, allowed_values).
			None is fine and gets serialised as an absent field.
		headers: Optional response headers (e.g. Retry-After on 429).
	"""
	body = ErrorResponse(error=message, code=code, details=details).model_dump(
		exclude_none=True,
	)
	raise HTTPException(status_code=status_code, detail=body, headers=headers)


def install_error_handler(app: FastAPI) -> None:
	"""Install a global HTTPException handler on ``app`` that normalises
	``detail`` into the canonical ErrorResponse shape.

	Called once from ``create_app``. Any ``HTTPException`` raised anywhere
	in the app — including by third-party middleware or by future
	oversights — is caught here and wrapped. The resulting response
	body always parses as ErrorResponse.
	"""

	@app.exception_handler(HTTPException)
	async def _wrap(_request: Request, exc: HTTPException):
		if isinstance(exc.detail, dict) and "error" in exc.detail and "code" in exc.detail:
			# Already canonical — emit as-is. validate to catch shape
			# drift but don't force exclude_none here (caller may have
			# intentionally included a null).
			body = ErrorResponse(**exc.detail).model_dump(exclude_none=True)
		elif isinstance(exc.detail, str):
			# String detail — wrap into the canonical shape with a
			# synthesized code based on the status family.
			body = ErrorResponse(
				error=exc.detail,
				code=_default_code_for(exc.status_code),
				details=None,
			).model_dump(exclude_none=True)
		else:
			# Unknown shape (list, None, ...). Fall back to a stringified
			# message so clients still see the canonical keys.
			body = ErrorResponse(
				error=str(exc.detail) if exc.detail else "error",
				code=_default_code_for(exc.status_code),
				details=None,
			).model_dump(exclude_none=True)
		return JSONResponse(
			status_code=exc.status_code,
			content=body,
			headers=dict(exc.headers or {}),
		)


def _default_code_for(status_code: int) -> str:
	"""Synthesize a code for stringy HTTPExceptions without one."""
	mapping = {
		400: "BAD_REQUEST",
		401: "UNAUTHORIZED",
		403: "FORBIDDEN",
		404: "NOT_FOUND",
		409: "CONFLICT",
		422: "UNPROCESSABLE_ENTITY",
		429: "RATE_LIMIT",
		500: "INTERNAL_ERROR",
		502: "BAD_GATEWAY",
		503: "SERVICE_UNAVAILABLE",
		504: "GATEWAY_TIMEOUT",
	}
	return mapping.get(status_code, f"HTTP_{status_code}")
