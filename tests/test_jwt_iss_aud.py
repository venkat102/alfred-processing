"""Tests for JWT iss/aud claim enforcement (TD-M1).

When both iss and aud are configured, a token issued for one Alfred
instance can no longer be replayed against another instance that
happens to share the signing key. When either is unset, behaviour is
backward-compatible with pre-TD-M1 tokens.
"""

from __future__ import annotations

import time

import jwt as _jwt
import pytest

from alfred.middleware.auth import create_jwt_token, verify_jwt_token

_KEY = "test-signing-key-32-plus-bytes-long-xxxxxxxx"
_USER = "u@test.com"
_ROLES = ["Administrator"]
_SITE = "site-a"


# ── Backward-compat (iss/aud unset) ───────────────────────────────


def test_no_enforcement_accepts_legacy_token_without_claims():
	# A pre-TD-M1 token has no iss/aud. verify_jwt_token with
	# issuer=None/audience=None must still accept it.
	legacy = create_jwt_token(_USER, _ROLES, _SITE, _KEY)
	payload = verify_jwt_token(legacy, _KEY, issuer=None, audience=None)
	assert payload["user"] == _USER


def test_no_enforcement_accepts_token_with_claims():
	# Conversely, a token that DOES carry iss/aud should still verify
	# when the server isn't enforcing them — just extra data that's ignored.
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="some-issuer", audience="some-audience",
	)
	payload = verify_jwt_token(token, _KEY)
	assert payload["user"] == _USER


# ── Enforcement ───────────────────────────────────────────────────


def test_enforcement_accepts_matching_iss_and_aud():
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="admin.example.com",
		audience="alfred.prod",
	)
	payload = verify_jwt_token(
		token, _KEY,
		issuer="admin.example.com",
		audience="alfred.prod",
	)
	assert payload["user"] == _USER


def test_enforcement_rejects_wrong_iss():
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="attacker.example",
		audience="alfred.prod",
	)
	with pytest.raises(ValueError, match="iss claim"):
		verify_jwt_token(
			token, _KEY,
			issuer="admin.example.com",
			audience="alfred.prod",
		)


def test_enforcement_rejects_wrong_aud():
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="admin.example.com",
		audience="alfred.staging",
	)
	with pytest.raises(ValueError, match="aud claim"):
		verify_jwt_token(
			token, _KEY,
			issuer="admin.example.com",
			audience="alfred.prod",
		)


def test_enforcement_rejects_token_without_iss():
	# Legacy token (no iss) hitting a server that NOW enforces iss.
	# This is the migration transition: legacy tokens die after the
	# flip. Operator-facing migration notes live in the rollout commit
	# message (TD-M1), not in a CHANGELOG - this repo doesn't maintain
	# one.
	legacy = create_jwt_token(_USER, _ROLES, _SITE, _KEY)
	with pytest.raises(ValueError, match="iss"):
		verify_jwt_token(
			legacy, _KEY,
			issuer="admin.example.com",
			audience="alfred.prod",
		)


def test_enforcement_rejects_token_without_aud():
	# Same as above for aud — a token that carries iss but not aud
	# must be rejected when aud is required.
	partial = _jwt.encode(
		{
			"user": _USER, "roles": _ROLES, "site_id": _SITE,
			"iss": "admin.example.com",
			"exp": int(time.time()) + 3600,
		},
		_KEY, algorithm="HS256",
	)
	with pytest.raises(ValueError, match="aud"):
		verify_jwt_token(
			partial, _KEY,
			issuer="admin.example.com",
			audience="alfred.prod",
		)


# ── Cross-instance replay prevention (the whole point) ────────────


def test_cross_instance_replay_blocked():
	# Instance A issues tokens with aud="alfred.prod". Attacker steals
	# one and tries to replay against Instance B (aud="alfred.staging"),
	# which shares the signing key for some misconfigured reason.
	# Without iss/aud this would work; with it, replay blocked.
	prod_token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="admin.example.com",
		audience="alfred.prod",
	)
	with pytest.raises(ValueError, match="aud"):
		verify_jwt_token(
			prod_token, _KEY,
			issuer="admin.example.com",
			audience="alfred.staging",
		)


# ── Only-one-set partial enforcement ──────────────────────────────


def test_only_issuer_enforced():
	# Operator configures JWT_ISSUER but leaves JWT_AUDIENCE empty —
	# the code path accepts this, enforces iss only.
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		issuer="admin.example.com",
	)
	# Missing aud is fine when we don't enforce aud.
	payload = verify_jwt_token(
		token, _KEY,
		issuer="admin.example.com",
	)
	assert payload["user"] == _USER


def test_only_audience_enforced():
	token = create_jwt_token(
		_USER, _ROLES, _SITE, _KEY,
		audience="alfred.prod",
	)
	payload = verify_jwt_token(
		token, _KEY,
		audience="alfred.prod",
	)
	assert payload["user"] == _USER
