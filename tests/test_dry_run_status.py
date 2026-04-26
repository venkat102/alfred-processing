"""Tests for #FLOW3: dry-run status distinguishes infra-error from invalid.

Before this split the UI couldn't tell "we couldn't validate" from
"changeset is invalid" - both returned `valid=False`. A user could
approve a changeset that had never actually been validated because
MCP was down, which defeats the whole point of the dry-run gate.

The new ``status`` field on the _dry_run_with_retry return value
carries one of: ok / invalid / infra_error / skipped. This module
exercises every code path that can produce each value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.agents.crew import CrewState
from alfred.api.websocket import _dry_run_with_retry


def _make_conn(*, mcp_result=None, mcp_raises=None):
	conn = MagicMock()
	conn.user = "u@x"
	conn.site_id = "t"
	mcp = MagicMock()
	if mcp_raises is not None:
		mcp.call_tool = AsyncMock(side_effect=mcp_raises)
	else:
		mcp.call_tool = AsyncMock(return_value=mcp_result)
	conn.mcp_client = mcp
	return conn


@pytest.mark.asyncio
async def test_skipped_when_no_mcp_client():
	conn = MagicMock()
	conn.mcp_client = None
	conn.user = "u@x"
	conn.site_id = "t"
	state = CrewState()

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "skipped"
	assert result["valid"] is True  # skip is treated as green-light (no bench to validate against)


@pytest.mark.asyncio
async def test_ok_when_mcp_returns_valid_true():
	conn = _make_conn(mcp_result={"valid": True, "issues": [], "validated": 3})
	result = await _dry_run_with_retry(
		conn, CrewState(), changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "ok"
	assert result["valid"] is True


@pytest.mark.asyncio
async def test_invalid_when_mcp_returns_valid_false_with_issues():
	conn = _make_conn(mcp_result={
		"valid": False,
		"issues": [{"severity": "blocker", "issue": "missing fieldname"}],
		"validated": 1,
	})
	# Short-circuit the self-heal retry by pre-setting dry_run_retries.
	state = CrewState()
	state.dry_run_retries = 1

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "invalid"
	assert result["valid"] is False
	assert len(result["issues"]) == 1


@pytest.mark.asyncio
async def test_infra_error_when_mcp_raises():
	conn = _make_conn(mcp_raises=ConnectionError("bench unreachable"))
	state = CrewState()
	state.dry_run_retries = 1  # skip retry

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "infra_error"
	assert result["valid"] is False
	assert any("infrastructure error" in i["issue"].lower() for i in result["issues"])


@pytest.mark.asyncio
async def test_infra_error_when_mcp_returns_error_wrapper():
	# Client-side _safe_execute returns {error: ..., message: ...} on
	# permission denied / not found / Frappe internal.
	conn = _make_conn(mcp_result={"error": "PermissionError", "message": "user cannot run dry_run_changeset"})
	state = CrewState()
	state.dry_run_retries = 1

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "infra_error"
	assert result["valid"] is False
	assert any("cannot run" in i["issue"] for i in result["issues"])


@pytest.mark.asyncio
async def test_infra_error_when_mcp_returns_non_dict():
	# Defensive: if MCP returns a string/None by accident, don't claim
	# the changeset is invalid - claim the validator is.
	conn = _make_conn(mcp_result="unexpected string")
	state = CrewState()
	state.dry_run_retries = 1

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "infra_error"
	assert result["valid"] is False


@pytest.mark.asyncio
async def test_status_survives_final_changes_assignment():
	# _dry_run_with_retry attaches _final_changes at the top-level
	# alongside status. Integration smoke: both fields coexist.
	conn = _make_conn(mcp_result={"valid": True, "issues": [], "validated": 1})
	result = await _dry_run_with_retry(
		conn, CrewState(), changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert "status" in result
	assert "_final_changes" in result
