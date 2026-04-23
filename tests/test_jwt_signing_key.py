"""Tests for JWT_SIGNING_KEY — TD-C2: separate REST bearer key from
JWT HMAC key so a leak of either cannot be used to forge both.

Covers:
  - Startup validation: same-as-API / short keys rejected.
  - Backward-compat: JWT_SIGNING_KEY unset falls back to API_SECRET_KEY
    and logs a deprecation warning.
  - Full separation: JWT_SIGNING_KEY set → JWTs signed with
    API_SECRET_KEY are rejected; only JWTs signed with the new key work.
"""

from __future__ import annotations

import logging
import os

import jwt as _jwt
import pytest

from alfred.main import create_app
from alfred.middleware.auth import create_jwt_token, verify_jwt_token


_STRONG_KEY_A = "a-very-long-and-unique-api-secret-key-32+chars"
_STRONG_KEY_B = "b-distinct-32-byte-jwt-signing-key-for-tests!"


def _boot_with(env_overrides: dict[str, str]):
	"""Boot the app with the given env overrides; return the app."""
	prior = {k: os.environ.get(k) for k in env_overrides}
	try:
		for k, v in env_overrides.items():
			if v is None:
				os.environ.pop(k, None)
			else:
				os.environ[k] = v
		from alfred.config import get_settings
		if hasattr(get_settings, "cache_clear"):
			get_settings.cache_clear()
		return create_app()
	finally:
		for k, v in prior.items():
			if v is None:
				os.environ.pop(k, None)
			else:
				os.environ[k] = v


# ── Startup validation ─────────────────────────────────────────────


def test_jwt_key_equal_to_api_key_rejected():
	with pytest.raises(ValueError, match="must NOT equal API_SECRET_KEY"):
		_boot_with({
			"API_SECRET_KEY": _STRONG_KEY_A,
			"JWT_SIGNING_KEY": _STRONG_KEY_A,   # same as API key
			"ALLOWED_ORIGINS": "https://example.com",
			"DEBUG": "false",
		})


def test_jwt_key_too_short_rejected():
	with pytest.raises(ValueError, match="at least 32"):
		_boot_with({
			"API_SECRET_KEY": _STRONG_KEY_A,
			"JWT_SIGNING_KEY": "short-key-12345",   # 15 bytes
			"ALLOWED_ORIGINS": "https://example.com",
			"DEBUG": "false",
		})


def test_jwt_key_empty_boots_with_fallback_warning(caplog):
	# Empty JWT_SIGNING_KEY = legacy shared-key mode. Must boot cleanly,
	# with a WARN log so operators see they should configure the new var.
	with caplog.at_level(logging.WARNING, logger="alfred.processing"):
		app = _boot_with({
			"API_SECRET_KEY": _STRONG_KEY_A,
			"JWT_SIGNING_KEY": "",
			"ALLOWED_ORIGINS": "https://example.com",
			"DEBUG": "false",
		})
	assert app is not None
	msgs = [r.message for r in caplog.records]
	assert any("JWT_SIGNING_KEY is not set" in m for m in msgs), msgs
	assert any("legacy shared-key" in m for m in msgs), msgs


def test_jwt_key_distinct_and_long_boots_cleanly(caplog):
	with caplog.at_level(logging.WARNING, logger="alfred.processing"):
		app = _boot_with({
			"API_SECRET_KEY": _STRONG_KEY_A,
			"JWT_SIGNING_KEY": _STRONG_KEY_B,
			"ALLOWED_ORIGINS": "https://example.com",
			"DEBUG": "false",
		})
	assert app is not None
	# NO fallback warning when the new key is set properly.
	msgs = [r.message for r in caplog.records]
	assert not any("legacy shared-key" in m for m in msgs), msgs


# ── Verification behaviour under each mode ─────────────────────────


def test_verify_fallback_uses_api_key_when_jwt_key_unset():
	# Unset JWT_SIGNING_KEY → verify_jwt_token at the call site uses
	# API_SECRET_KEY. Simulate the call-site choice directly.
	signing_key = "" or _STRONG_KEY_A   # mirrors the fallback expression
	token = create_jwt_token(
		user="u@test.com", roles=["System Manager"],
		site_id="site-a", secret_key=signing_key,
	)
	payload = verify_jwt_token(token, signing_key)
	assert payload["user"] == "u@test.com"


def test_verify_split_mode_rejects_api_key_signed_jwt():
	# When JWT_SIGNING_KEY is set and DIFFERENT from API_SECRET_KEY, a
	# JWT signed with API_SECRET_KEY must NOT verify. This is the
	# substance of the key-split security property.
	stale_token = create_jwt_token(
		user="u@test.com", roles=["Administrator"],
		site_id="site-a", secret_key=_STRONG_KEY_A,   # signed with API key
	)
	with pytest.raises(ValueError, match="signature"):
		verify_jwt_token(stale_token, _STRONG_KEY_B)   # verified with JWT key


def test_verify_split_mode_accepts_jwt_key_signed_jwt():
	fresh_token = create_jwt_token(
		user="u@test.com", roles=["Administrator"],
		site_id="site-a", secret_key=_STRONG_KEY_B,   # signed with JWT key
	)
	payload = verify_jwt_token(fresh_token, _STRONG_KEY_B)
	assert payload["user"] == "u@test.com"


def test_verify_split_mode_api_key_cannot_be_used_to_forge_jwt():
	# The attack this defends against: attacker has API_SECRET_KEY
	# (e.g. leaked via a log spill) and tries to craft a JWT. In split
	# mode, the processing app rejects because it verifies with
	# JWT_SIGNING_KEY.
	forged = _jwt.encode(
		{"user": "attacker@evil", "roles": ["Administrator"],
		 "site_id": "target-site", "exp": 9999999999},
		_STRONG_KEY_A,   # attacker only has this
		algorithm="HS256",
	)
	with pytest.raises(ValueError):
		verify_jwt_token(forged, _STRONG_KEY_B)   # but server uses this


# ── Boundary: exact 32-byte key boundary accepted ──────────────────


def test_jwt_key_exactly_32_chars_boots():
	key_32 = "A" * 32
	app = _boot_with({
		"API_SECRET_KEY": _STRONG_KEY_A,
		"JWT_SIGNING_KEY": key_32,
		"ALLOWED_ORIGINS": "https://example.com",
		"DEBUG": "false",
	})
	assert app is not None


def test_jwt_key_31_chars_rejected():
	key_31 = "A" * 31
	with pytest.raises(ValueError, match="at least 32"):
		_boot_with({
			"API_SECRET_KEY": _STRONG_KEY_A,
			"JWT_SIGNING_KEY": key_31,
			"ALLOWED_ORIGINS": "https://example.com",
			"DEBUG": "false",
		})
