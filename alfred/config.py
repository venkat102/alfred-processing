"""Environment configuration for Alfred Processing App.

All configuration is centralized here via Pydantic Settings.
No other module should read environment variables directly.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
	"""Application settings loaded from environment variables."""

	# Server
	HOST: str = "0.0.0.0"
	PORT: int = 8000
	WORKERS: int = 4
	DEBUG: bool = False

	# Security
	API_SECRET_KEY: str

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

	# CORS — restrict in production, default allows all for dev
	ALLOWED_ORIGINS: str = "*"

	# WebSocket
	WS_HEARTBEAT_INTERVAL: int = 30
	WS_MESSAGE_BUFFER_TTL: int = 3600
	WS_MESSAGE_BUFFER_MAX: int = 1000

	model_config = {
		"env_file": ".env",
		"env_file_encoding": "utf-8",
		"case_sensitive": True,
	}


def get_settings() -> Settings:
	"""Get application settings. Raises ValidationError if required env vars are missing."""
	return Settings()
