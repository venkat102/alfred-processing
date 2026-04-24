"""Environment configuration for Alfred Processing App.

All configuration is centralized here via Pydantic Settings.
No other module should read environment variables directly.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings

# Minimum acceptable API_SECRET_KEY length (bytes). JWT HS256 wants >= 32
# (RFC 7518 Section 3.2), and the FastAPI Bearer check uses the same key,
# so 32 is the floor for both. Shorter keys let us boot today - we now
# refuse to boot, to close that gap.
_API_SECRET_KEY_MIN_LENGTH = 32

# Values we've seen in the wild as copy-paste defaults from tutorials,
# .env.example files, and "I'll fix it later" dev placeholders. Rejecting
# these at boot prevents an operator from shipping with a guessable key.
_API_SECRET_KEY_FORBIDDEN = frozenset({
	"",
	"changeme",
	"change-me",
	"changethis",
	"change-this",
	"secret",
	"password",
	"dev",
	"devsecret",
	"dev-secret",
	"test",
	"testsecret",
	"test-secret",
	"your-secret-key",
	"your_secret_key",
	"supersecret",
	"super-secret",
})


class Settings(BaseSettings):
	"""Application settings loaded from environment variables."""

	# Server
	HOST: str = "0.0.0.0"
	PORT: int = 8000
	WORKERS: int = 4
	DEBUG: bool = False

	# Security
	API_SECRET_KEY: str

	@field_validator("API_SECRET_KEY")
	@classmethod
	def _validate_api_secret_key(cls, v: str) -> str:
		"""Refuse to boot on a weak or default API_SECRET_KEY.

		Rejects anything shorter than 32 bytes (the HS256 floor) and a
		hardcoded list of common placeholder values. The message points
		operators at the rotation script so they can fix it without
		hand-editing .env.
		"""
		if v is None or not isinstance(v, str):
			raise ValueError(
				"API_SECRET_KEY is required. "
				"Run: python scripts/rotate_api_secret_key.py"
			)
		if v.lower() in _API_SECRET_KEY_FORBIDDEN:
			raise ValueError(
				"API_SECRET_KEY is set to a known-weak placeholder value "
				f"({v!r}). Generate a strong key: "
				"python scripts/rotate_api_secret_key.py"
			)
		if len(v) < _API_SECRET_KEY_MIN_LENGTH:
			raise ValueError(
				f"API_SECRET_KEY is too short ({len(v)} chars); "
				f"minimum is {_API_SECRET_KEY_MIN_LENGTH}. "
				"Generate a strong key: "
				"python scripts/rotate_api_secret_key.py"
			)
		return v

	# Redis
	REDIS_URL: str = "redis://redis:6379/0"
	REDIS_POOL_SIZE: int = 20
	REDIS_SOCKET_TIMEOUT: int = 5

	# Fallback LLM (used when client doesn't provide its own LLM config)
	FALLBACK_LLM_MODEL: str = ""
	FALLBACK_LLM_API_KEY: str = ""
	FALLBACK_LLM_BASE_URL: str = ""

	# Admin Portal
	ADMIN_PORTAL_URL: str = ""
	ADMIN_SERVICE_KEY: str = ""

	# CORS - restrict in production, default allows all for dev
	ALLOWED_ORIGINS: str = "*"

	# WebSocket
	WS_HEARTBEAT_INTERVAL: int = 30

	model_config = {
		"env_file": ".env",
		"env_file_encoding": "utf-8",
		"case_sensitive": True,
		# Ignore unknown env vars instead of rejecting them. The ALFRED_*
		# feature flags (ALFRED_ORCHESTRATOR_ENABLED,
		# ALFRED_REFLECTION_ENABLED, ALFRED_TRACING_ENABLED, etc.) are
		# read at usage sites via os.environ.get() rather than threaded
		# through Settings - they're runtime toggles, not config values.
		# Without this, adding any new feature flag to .env would crash
		# the pipeline at startup with ValidationError(extra_forbidden).
		"extra": "ignore",
	}


def get_settings() -> Settings:
	"""Get application settings. Raises ValidationError if required env vars are missing."""
	return Settings()
