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


# ── Schema-grounding pre-check tests ────────────────────────────


def _make_conn_per_tool(per_tool: dict):
	"""Conn with a side_effect that dispatches by tool name.

	`per_tool` maps tool name -> result dict (or callable returning a result).
	Used by the pre-check tests where we need validate_changeset and
	dry_run_changeset to return different things in the same run.
	"""
	conn = MagicMock()
	conn.user = "u@x"
	conn.site_id = "t"
	mcp = MagicMock()

	async def dispatch(tool_name, args):  # noqa: ARG001
		if tool_name not in per_tool:
			raise AssertionError(f"unexpected tool call: {tool_name}")
		val = per_tool[tool_name]
		return val(args) if callable(val) else val

	mcp.call_tool = AsyncMock(side_effect=dispatch)
	conn.mcp_client = mcp
	return conn


@pytest.mark.asyncio
async def test_pre_check_short_circuits_when_validate_changeset_flags_critical():
	"""validate_changeset returning valid=False must trigger the retry path
	without ever calling dry_run_changeset. The pre-check is cheaper, so we
	short-circuit on it - the savepoint round trip is wasted otherwise."""
	pre_issue = {
		"severity": "critical", "item_index": 0,
		"doctype": "Custom Field", "code": "duplicate_field",
		"message": "Field 'priority' already exists on 'ToDo'",
		"fix_hint": "Pick a different fieldname.",
	}
	# dry_run_changeset must NOT be called - if it is, the dispatcher raises.
	conn = _make_conn_per_tool({
		"validate_changeset": {"valid": False, "issues": [pre_issue], "checked": 1},
	})
	state = CrewState()
	state.dry_run_retries = 1  # skip the developer retry kickoff path

	result = await _dry_run_with_retry(
		conn, state, changes=[{"op": "create", "doctype": "Custom Field"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["valid"] is False
	assert result["status"] == "invalid"
	assert len(result["issues"]) == 1
	# Pre-check issues get normalized into the same shape as savepoint issues.
	issue = result["issues"][0]
	assert issue["severity"] == "critical"
	assert issue["code"] == "duplicate_field"
	assert "priority" in issue["issue"]


@pytest.mark.asyncio
async def test_pre_check_falls_through_when_validate_changeset_clean():
	"""When validate_changeset returns valid=True, the savepoint dry-run
	must still run - the pre-check is additive, not a replacement."""
	dry_called = {"n": 0}

	def _dry(args):  # noqa: ARG001
		dry_called["n"] += 1
		return {"valid": True, "issues": [], "validated": 1}

	conn = _make_conn_per_tool({
		"validate_changeset": {"valid": True, "issues": [], "checked": 1},
		"dry_run_changeset": _dry,
	})
	result = await _dry_run_with_retry(
		conn, CrewState(), changes=[{"op": "create", "doctype": "Custom Field"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "ok"
	assert dry_called["n"] == 1, "dry_run_changeset must run when pre-check is clean"


@pytest.mark.asyncio
async def test_pre_check_infra_failure_falls_through_to_dry_run():
	"""If the validate_changeset MCP call itself errors (infra failure, not
	a content verdict), skip the pre-check and run the savepoint dry-run.
	Never block on infra."""
	dry_called = {"n": 0}

	def _dry(args):  # noqa: ARG001
		dry_called["n"] += 1
		return {"valid": True, "issues": [], "validated": 1}

	# validate_changeset comes back with the {error, message} shape that the
	# alfred_client _safe_execute wrapper emits on internal_error.
	conn = _make_conn_per_tool({
		"validate_changeset": {"error": "internal_error", "message": "boom"},
		"dry_run_changeset": _dry,
	})
	result = await _dry_run_with_retry(
		conn, CrewState(), changes=[{"op": "create", "doctype": "Custom Field"}],
		site_config={}, event_callback=AsyncMock(),
	)
	# Pre-check infra failure should not be treated as a content verdict.
	# It should fall through to the savepoint dry-run, which says ok.
	assert result["status"] == "ok"
	assert dry_called["n"] == 1


@pytest.mark.asyncio
async def test_pre_check_only_warnings_falls_through():
	"""validate_changeset returning valid=True with only `warning` severity
	issues is still a green pre-check. Don't promote warnings to retry."""
	dry_called = {"n": 0}

	def _dry(args):  # noqa: ARG001
		dry_called["n"] += 1
		return {"valid": True, "issues": [], "validated": 1}

	# Note: validate_changeset's contract is `valid` is False only for
	# critical issues. Warnings can coexist with valid=True.
	conn = _make_conn_per_tool({
		"validate_changeset": {
			"valid": True, "checked": 1,
			"issues": [{"severity": "warning", "code": "warn", "message": "minor"}],
		},
		"dry_run_changeset": _dry,
	})
	result = await _dry_run_with_retry(
		conn, CrewState(), changes=[{"op": "create"}],
		site_config={}, event_callback=AsyncMock(),
	)
	assert result["status"] == "ok"
	assert dry_called["n"] == 1
