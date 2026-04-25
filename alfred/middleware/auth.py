"""Authentication middleware for API key and JWT verification.

API key validation is required on every REST request and WebSocket handshake.
JWT verification is required for WebSocket connections to extract user identity
and site_id for namespace isolation.
"""

import hmac
import logging
import time

import jwt
from fastapi import Depends, Header, HTTPException, Request
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

	# Constant-time comparison: `!=` short-circuits on first differing byte,
	# which leaks key-byte positions via response-latency timing. Attacker
	# with network access can brute-force the key one byte at a time. Use
	# hmac.compare_digest so the comparison runs in O(len(input)) regardless
	# of where the mismatch is. Encode to bytes explicitly so the compare
	# is byte-level rather than relying on str-level Unicode equivalence.
	if not hmac.compare_digest(
		api_key.encode("utf-8"), expected_key.encode("utf-8"),
	):
		logger.warning("Invalid API key from %s", request.client.host if request.client else "unknown")
		raise HTTPException(
			status_code=401,
			detail={"error": "Invalid API key", "code": "AUTH_INVALID"},
		)

	return api_key


def _get_jwt_signing_key(request: Request) -> str:
	"""Resolve the secret used to verify REST JWTs.

	Same fallback chain as the WS handshake (see ``connection.py``):
	prefer ``JWT_SIGNING_KEY`` once it's configured; otherwise fall back
	to ``API_SECRET_KEY`` for the legacy shared-key mode TD-C2 is
	gradually phasing out.
	"""
	settings = request.app.state.settings
	return settings.JWT_SIGNING_KEY or settings.API_SECRET_KEY


async def verify_rest_jwt(
	request: Request,
	x_jwt: str | None = Header(default=None, alias="X-Alfred-JWT"),
) -> dict:
	"""Dependency that validates a per-user JWT for REST endpoints.

	Without this, ``POST /api/v1/tasks`` trusted ``site_id`` /
	``user`` straight from the request body — a leaked
	``API_SECRET_KEY`` would let any caller submit tasks as any
	tenant. Now every REST handler that needs site/user context
	resolves them from the JWT instead, so the body's ``site_config.site_id``
	is enforced to match what the JWT claims.

	The JWT is read from the ``X-Alfred-JWT`` header (rather than
	``Authorization``) so the existing ``verify_api_key`` Bearer flow
	is preserved as a service-level gate. Layered:
	  1. ``Authorization: Bearer <api_key>`` proves the *caller is an
	     authorised client app* (``verify_api_key``).
	  2. ``X-Alfred-JWT: <jwt>`` proves *which user / site* is acting
	     on this request (this dependency).

	Returns:
		The decoded JWT payload: ``{user, roles, site_id, iat, exp}``.

	Raises:
		HTTPException 401 if the header is missing, malformed, expired,
		or fails signature / claims verification.
	"""
	if not x_jwt:
		logger.warning(
			"Missing X-Alfred-JWT header from %s",
			request.client.host if request.client else "unknown",
		)
		raise HTTPException(
			status_code=401,
			detail={
				"error": (
					"Missing X-Alfred-JWT header. REST endpoints require a "
					"per-user JWT in addition to the service Bearer token."
				),
				"code": "JWT_MISSING",
			},
		)

	secret = _get_jwt_signing_key(request)
	try:
		payload = verify_jwt_token(x_jwt, secret)
	except ValueError as e:
		logger.warning(
			"REST JWT verification failed from %s: %s",
			request.client.host if request.client else "unknown", e,
		)
		raise HTTPException(
			status_code=401,
			detail={"error": str(e), "code": "JWT_INVALID"},
		) from e

	return payload


def verify_jwt_token(
	token: str,
	secret_key: str,
	*,
	issuer: str | None = None,
	audience: str | None = None,
) -> dict:
	"""Verify a JWT token and extract the payload.

	Args:
		token: The JWT token string.
		secret_key: The secret key used to sign the token.
		issuer: If provided, require the token's ``iss`` claim to match.
			TD-M1 — prevents token-replay across Alfred instances that
			share a signing key. Unset = no enforcement (backward-compat).
		audience: If provided, require the token's ``aud`` claim to
			match. Same rationale as issuer.

	Returns:
		Dict with keys: user, roles, site_id, iat, exp.

	Raises:
		ValueError: If the token is empty, expired, tampered, signed with a
			different algorithm, missing any required claim (user, roles,
			site_id, exp), or fails iss/aud check when those are enforced.
	"""
	if not token:
		raise ValueError("JWT token is empty")

	# Build PyJWT's decode options: always require exp; require iss/aud
	# when the caller is enforcing them (so a missing claim doesn't
	# silently pass). When NOT enforcing, we also have to set
	# verify_iss/verify_aud=False — otherwise a token that happens to
	# carry those claims fails verification because PyJWT has no
	# expected value to compare against.
	required: list[str] = ["exp"]
	options: dict = {"require": required}
	if issuer:
		required.append("iss")
	else:
		options["verify_iss"] = False
	if audience:
		required.append("aud")
	else:
		options["verify_aud"] = False

	decode_kwargs: dict = {
		"algorithms": ["HS256"],
		"options": options,
	}
	if audience:
		decode_kwargs["audience"] = audience
	if issuer:
		decode_kwargs["issuer"] = issuer

	try:
		payload = jwt.decode(token, secret_key, **decode_kwargs)
	except jwt.ExpiredSignatureError:
		raise ValueError("JWT token has expired")
	except jwt.InvalidSignatureError:
		raise ValueError("JWT signature verification failed - token may be tampered")
	except jwt.InvalidIssuerError:
		raise ValueError(f"JWT iss claim does not match expected issuer {issuer!r}")
	except jwt.InvalidAudienceError:
		raise ValueError(f"JWT aud claim does not match expected audience {audience!r}")
	except jwt.MissingRequiredClaimError as e:
		raise ValueError(f"JWT missing required claim: {e.claim}")
	except jwt.DecodeError:
		raise ValueError("JWT token is malformed")
	except jwt.InvalidTokenError as e:
		raise ValueError(f"Invalid JWT token: {e}")

	# Validate required claims (PyJWT only enforces exp via options["require"])
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
		"exp": payload["exp"],
	}


def create_jwt_token(
	user: str,
	roles: list[str],
	site_id: str,
	secret_key: str,
	exp_hours: int = 24,
	*,
	issuer: str | None = None,
	audience: str | None = None,
) -> str:
	"""Create a signed JWT token. Used for testing and by the client app.

	Args:
		user: User email.
		roles: List of user roles.
		site_id: Customer site identifier.
		secret_key: Secret key for signing.
		exp_hours: Token validity in hours.
		issuer: If provided, include as ``iss`` claim. TD-M1 — pair with
			the issuer passed to verify_jwt_token on the verifier side.
		audience: If provided, include as ``aud`` claim. Same rationale.

	Returns:
		Signed JWT token string.
	"""
	now = int(time.time())
	payload: dict = {
		"user": user,
		"roles": roles,
		"site_id": site_id,
		"iat": now,
		"exp": now + (exp_hours * 3600),
	}
	if issuer:
		payload["iss"] = issuer
	if audience:
		payload["aud"] = audience
	return jwt.encode(payload, secret_key, algorithm="HS256")
