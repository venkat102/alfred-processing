"""Tests for CORS startup-time validation.

The previous config shipped allow_origins=["*"] with allow_credentials=True,
which is invalid per the CORS spec — browsers reject credentialed requests
when origin is `*`, so the credential path silently never worked. Fix
forces an explicit allow-list and fails the app at boot if `*` is used.
See TD-C5 in docs/tech-debt-backlog.md.
"""

from __future__ import annotations

import os

import pytest

from alfred.main import create_app


def _boot_with(env_overrides: dict[str, str]):
	"""Boot the app with the given env overrides; return the app."""
	prior = {k: os.environ.get(k) for k in env_overrides}
	try:
		for k, v in env_overrides.items():
			if v is None:
				os.environ.pop(k, None)
			else:
				os.environ[k] = v
		# Settings is @lru_cache'd on get_settings; clear it so each test
		# boots cleanly.
		from alfred.config import get_settings
		get_settings.cache_clear() if hasattr(get_settings, "cache_clear") else None
		return create_app()
	finally:
		for k, v in prior.items():
			if v is None:
				os.environ.pop(k, None)
			else:
				os.environ[k] = v


# ── Startup rejection ──────────────────────────────────────────────


def test_star_origin_rejected_when_debug_false():
	with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
		_boot_with({
			"API_SECRET_KEY": "k",
			"ALLOWED_ORIGINS": "*",
			"DEBUG": "false",
		})


def test_empty_origin_rejected_at_startup():
	with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
		_boot_with({
			"API_SECRET_KEY": "k",
			"ALLOWED_ORIGINS": "",
			"DEBUG": "false",
		})


def test_whitespace_only_origin_rejected_at_startup():
	with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
		_boot_with({
			"API_SECRET_KEY": "k",
			"ALLOWED_ORIGINS": "   ",
			"DEBUG": "false",
		})


def test_only_commas_and_spaces_rejected_at_startup():
	# Parses to an empty list after stripping — still invalid.
	with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
		_boot_with({
			"API_SECRET_KEY": "k",
			"ALLOWED_ORIGINS": ",,, ,  ",
			"DEBUG": "false",
		})


# ── DEBUG-mode escape hatch ────────────────────────────────────────


def test_debug_mode_allows_star_with_credentials_disabled():
	# With DEBUG=true, `*` is accepted as a dev convenience, but
	# allow_credentials MUST be False so the config stays CORS-spec
	# compliant (browsers reject credentialed `*`).
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "*",
		"DEBUG": "true",
	})
	cors = _extract_cors(app)
	assert cors.options["allow_origins"] == ["*"]
	assert cors.options["allow_credentials"] is False


def test_debug_mode_with_explicit_origins_uses_credentials():
	# DEBUG=true + an explicit list should behave like prod (credentials
	# enabled, methods/headers tightened), NOT fall into the wildcard
	# escape path. The escape only fires when origin == "*".
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "http://localhost:8001",
		"DEBUG": "true",
	})
	cors = _extract_cors(app)
	assert cors.options["allow_origins"] == ["http://localhost:8001"]
	assert cors.options["allow_credentials"] is True
	assert "PUT" not in cors.options["allow_methods"]


def test_debug_mode_with_empty_origins_still_rejected():
	# DEBUG=true shouldn't rescue a totally missing config - "" is
	# almost certainly an operator mistake, not a dev-intent wildcard.
	with pytest.raises(ValueError, match="ALLOWED_ORIGINS"):
		_boot_with({
			"API_SECRET_KEY": "k",
			"ALLOWED_ORIGINS": "",
			"DEBUG": "true",
		})


# ── Valid configurations boot successfully ─────────────────────────


def test_single_origin_boots():
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "https://example.com",
		"DEBUG": "false",
	})
	assert app is not None


def test_multiple_origins_boot():
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "http://localhost:8001,https://app.example.com",
		"DEBUG": "false",
	})
	# Pull the CORS middleware off the stack and assert origins parsed right.
	cors = _extract_cors(app)
	assert "http://localhost:8001" in cors.options["allow_origins"]
	assert "https://app.example.com" in cors.options["allow_origins"]


def test_origins_are_trimmed():
	# Whitespace around commas shouldn't break anything.
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "  http://localhost:8001  ,  https://app.example.com  ",
		"DEBUG": "false",
	})
	cors = _extract_cors(app)
	assert "http://localhost:8001" in cors.options["allow_origins"]
	assert "https://app.example.com" in cors.options["allow_origins"]


# ── Tightened methods + headers ────────────────────────────────────


def test_methods_restricted_to_get_post_options():
	# `*` previously allowed every method; we only use GET / POST /
	# OPTIONS. Enforcing the list shrinks the pre-approved attack
	# surface per origin.
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "https://example.com",
		"DEBUG": "false",
	})
	cors = _extract_cors(app)
	methods = cors.options["allow_methods"]
	assert "GET" in methods
	assert "POST" in methods
	assert "OPTIONS" in methods
	assert "PUT" not in methods
	assert "DELETE" not in methods
	assert "PATCH" not in methods
	assert "*" not in methods


def test_headers_restricted_to_known_list():
	app = _boot_with({
		"API_SECRET_KEY": "k",
		"ALLOWED_ORIGINS": "https://example.com",
		"DEBUG": "false",
	})
	cors = _extract_cors(app)
	headers = cors.options["allow_headers"]
	assert "Authorization" in headers
	assert "Content-Type" in headers
	assert "*" not in headers


# ── Helpers ────────────────────────────────────────────────────────


def _extract_cors(app):
	"""Pull the CORSMiddleware instance's options off the app's middleware stack."""
	for m in app.user_middleware:
		name = getattr(m.cls, "__name__", "")
		if "CORSMiddleware" in name:
			# Starlette stores kwargs on `m.kwargs` (newer) or on `m.options` / `m.args`.
			return _MiddlewareView(
				options={**(getattr(m, "kwargs", None) or {})},
			)
	raise AssertionError("CORSMiddleware not found on app.user_middleware")


class _MiddlewareView:
	"""Tiny wrapper so tests can access .options regardless of starlette version."""
	def __init__(self, options: dict):
		self.options = options
