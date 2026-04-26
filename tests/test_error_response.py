"""Tests for the unified error response shape (TD-M2)."""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from alfred.api.errors import raise_error
from alfred.main import create_app
from alfred.models.messages import ErrorResponse


@pytest.fixture
async def app():
	os.environ["API_SECRET_KEY"] = "test-cors-32byte-key-not-a-real-secret-padding"
	os.environ["ALLOWED_ORIGINS"] = "http://localhost"
	os.environ["DEBUG"] = "false"
	from alfred.config import get_settings
	if hasattr(get_settings, "cache_clear"):
		get_settings.cache_clear()

	a = create_app()
	a.state.settings = get_settings()

	# Register a couple of routes that raise different error shapes, so
	# we can prove the handler normalises all of them.
	@a.get("/test/dict-detail")
	def _dict_detail():
		raise HTTPException(status_code=404, detail={"error": "nope", "code": "NO_NOPE"})

	@a.get("/test/string-detail")
	def _string_detail():
		raise HTTPException(status_code=404, detail="just a string")

	@a.get("/test/raise-error-helper")
	def _helper():
		raise_error(429, "RATE_LIMIT", "too many", details={"retry_after": 30})

	@a.get("/test/none-detail")
	def _none_detail():
		raise HTTPException(status_code=500)

	yield a


@pytest.fixture
async def client(app):
	async with AsyncClient(
		transport=ASGITransport(app=app), base_url="http://test",
	) as ac:
		yield ac


# ── Canonical shape for all error paths ───────────────────────────


async def test_dict_detail_passes_through(client):
	resp = await client.get("/test/dict-detail")
	assert resp.status_code == 404
	body = resp.json()
	assert body == {"error": "nope", "code": "NO_NOPE"}


async def test_string_detail_wrapped(client):
	resp = await client.get("/test/string-detail")
	assert resp.status_code == 404
	body = resp.json()
	# Handler must synthesize a code based on status.
	assert body["error"] == "just a string"
	assert body["code"] == "NOT_FOUND"


async def test_raise_error_helper(client):
	resp = await client.get("/test/raise-error-helper")
	assert resp.status_code == 429
	body = resp.json()
	assert body["error"] == "too many"
	assert body["code"] == "RATE_LIMIT"
	assert body["details"] == {"retry_after": 30}


async def test_none_detail_handled(client):
	# Some HTTPException paths raise without a detail — handler must
	# still emit a valid ErrorResponse rather than crash.
	resp = await client.get("/test/none-detail")
	assert resp.status_code == 500
	body = resp.json()
	assert "error" in body
	assert body["code"] == "INTERNAL_ERROR"


# ── ErrorResponse model ───────────────────────────────────────────


def test_error_response_schema():
	er = ErrorResponse(error="x", code="Y", details={"a": 1})
	assert er.error == "x"
	assert er.code == "Y"
	assert er.details == {"a": 1}


def test_error_response_details_optional():
	er = ErrorResponse(error="x", code="Y")
	assert er.details is None


def test_error_response_excludes_none_on_dump():
	# exclude_none is how install_error_handler + raise_error serialise;
	# a null `details` should not appear on the wire.
	er = ErrorResponse(error="x", code="Y")
	body = er.model_dump(exclude_none=True)
	assert "details" not in body


# ── Helper function directly ──────────────────────────────────────


def test_raise_error_raises_httpexception():
	with pytest.raises(HTTPException) as exc:
		raise_error(400, "BAD_REQ", "bad")
	assert exc.value.status_code == 400
	assert exc.value.detail["code"] == "BAD_REQ"


def test_raise_error_passes_headers():
	with pytest.raises(HTTPException) as exc:
		raise_error(429, "RATE", "slow down", headers={"Retry-After": "60"})
	assert exc.value.headers == {"Retry-After": "60"}
