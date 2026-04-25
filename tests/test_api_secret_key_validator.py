"""Tests for the API_SECRET_KEY startup validator in alfred.config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from alfred.config import Settings, _API_SECRET_KEY_MIN_LENGTH


def _strong_key(n: int = 48) -> str:
	return "x" * n


class TestApiSecretKeyValidator:
	def test_accepts_key_at_minimum_length(self):
		Settings(API_SECRET_KEY=_strong_key(_API_SECRET_KEY_MIN_LENGTH))

	def test_accepts_long_strong_key(self):
		Settings(API_SECRET_KEY=_strong_key(64))

	def test_rejects_empty_string(self):
		with pytest.raises(ValidationError) as exc:
			Settings(API_SECRET_KEY="")
		assert "rotate_api_secret_key" in str(exc.value)

	def test_rejects_short_key(self):
		short = _strong_key(_API_SECRET_KEY_MIN_LENGTH - 1)
		with pytest.raises(ValidationError) as exc:
			Settings(API_SECRET_KEY=short)
		assert "too short" in str(exc.value)

	@pytest.mark.parametrize("placeholder", [
		"changeme", "change-me", "changethis", "change-this",
		"secret", "password", "dev", "devsecret", "dev-secret",
		"test", "testsecret", "test-secret",
		"your-secret-key", "your_secret_key",
		"supersecret", "super-secret",
	])
	def test_rejects_known_weak_placeholders(self, placeholder: str):
		with pytest.raises(ValidationError) as exc:
			Settings(API_SECRET_KEY=placeholder)
		assert "weak placeholder" in str(exc.value)

	def test_rejects_placeholder_case_insensitive(self):
		with pytest.raises(ValidationError):
			Settings(API_SECRET_KEY="CHANGEME")
		with pytest.raises(ValidationError):
			Settings(API_SECRET_KEY="Secret")
