"""Authentication middleware for API key and JWT verification.

API key validation is required on every REST request and WebSocket handshake.
JWT verification is required for WebSocket connections to extract user identity
and site_id for namespace isolation.
"""

import logging
import time

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("alfred.auth")

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_api_secret_key(request: Request) -> str:
	"""Get the API secret key from app settings."""
	return request.app.state.settings.API_SECRET_KEY


async def verify_api_key(
	request: Request,
	credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
	"""FastAPI dependency that validates the API key from the Authorization header.

	Expected format: Authorization: Bearer <api_key>

	Returns:
		The validated API key string.

	Raises:
		HTTPException 401 if the key is missing, malformed, or invalid.
	"""
	if credentials is None:
		logger.warning("Missing Authorization header from %s", request.client.host if request.client else "unknown")
		raise HTTPException(
			status_code=401,
			detail={"error": "Missing Authorization header. Expected: Bearer <api_key>", "code": "AUTH_MISSING"},
		)

	api_key = credentials.credentials
	expected_key = _get_api_secret_key(request)

	if api_key != expected_key:
		logger.warning("Invalid API key from %s", request.client.host if request.client else "unknown")
		raise HTTPException(
			status_code=401,
			detail={"error": "Invalid API key", "code": "AUTH_INVALID"},
		)

	return api_key


def verify_jwt_token(token: str, secret_key: str) -> dict:
	"""Verify a JWT token and extract the payload.

	Args:
		token: The JWT token string.
		secret_key: The secret key used to sign the token.

	Returns:
		Dict with keys: user, roles, site_id, iat.

	Raises:
		ValueError: If the token is expired, tampered, or missing required claims.
	"""
	try:
		payload = jwt.decode(token, secret_key, algorithms=["HS256"])
	except jwt.ExpiredSignatureError:
		raise ValueError("JWT token has expired")
	except jwt.InvalidSignatureError:
		raise ValueError("JWT signature verification failed - token may be tampered")
	except jwt.DecodeError:
		raise ValueError("JWT token is malformed")
	except jwt.InvalidTokenError as e:
		raise ValueError(f"Invalid JWT token: {e}")

	# Validate required claims
	required_claims = ["user", "roles", "site_id"]
	missing = [c for c in required_claims if c not in payload]
	if missing:
		raise ValueError(f"JWT missing required claims: {', '.join(missing)}")

	if not payload.get("site_id"):
		raise ValueError("JWT site_id claim cannot be empty")

	return {
		"user": payload["user"],
		"roles": payload["roles"],
		"site_id": payload["site_id"],
		"iat": payload.get("iat", 0),
	}


def create_jwt_token(user: str, roles: list[str], site_id: str, secret_key: str, exp_hours: int = 24) -> str:
	"""Create a signed JWT token. Used for testing and by the client app.

	Args:
		user: User email.
		roles: List of user roles.
		site_id: Customer site identifier.
		secret_key: Secret key for signing.
		exp_hours: Token validity in hours.

	Returns:
		Signed JWT token string.
	"""
	now = int(time.time())
	payload = {
		"user": user,
		"roles": roles,
		"site_id": site_id,
		"iat": now,
		"exp": now + (exp_hours * 3600),
	}
	return jwt.encode(payload, secret_key, algorithm="HS256")
