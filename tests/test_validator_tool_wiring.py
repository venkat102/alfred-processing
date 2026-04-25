"""Pin the wiring of the deterministic validators into agent tool bundles.

These were defined in ``alfred/tools/code_validation.py`` and
``alfred/tools/permission_checks.py`` but never registered with any
agent — the audit's M1. Keep these tests around so a future refactor
that drops the imports surfaces the regression instead of silently
shipping the stubs again.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mcp_bundles():
	from alfred.tools.mcp_tools import build_mcp_tools
	return build_mcp_tools(MagicMock())


def _names(tools) -> set[str]:
	return {getattr(t, "name", "") for t in tools}


class TestTesterBundle:
	"""The Tester agent runs static validation on what the Developer
	produced — so it needs the deep checks, not just ``compile()``."""

	def test_tester_has_real_python_validator(self, mcp_bundles):
		assert "validate_python_syntax_tool" in _names(mcp_bundles["tester"])

	def test_tester_has_real_js_validator(self, mcp_bundles):
		assert "validate_js_syntax_tool" in _names(mcp_bundles["tester"])

	def test_tester_has_doctype_validator(self, mcp_bundles):
		assert "validate_doctype_tool" in _names(mcp_bundles["tester"])

	def test_tester_has_workflow_validator(self, mcp_bundles):
		assert "validate_workflow_tool" in _names(mcp_bundles["tester"])

	def test_tester_has_changeset_order_validator(self, mcp_bundles):
		assert "validate_changeset_order_tool" in _names(mcp_bundles["tester"])

	def test_tester_keeps_stubs_for_backward_compat(self, mcp_bundles):
		"""Stubs stay registered alongside the real tools so the agent
		can still pick the cheap one for code that's already been
		parsed and accepted, and so legacy tests that grep by name
		don't break."""
		names = _names(mcp_bundles["tester"])
		assert "validate_python_syntax_stub" in names
		assert "validate_js_syntax_stub" in names


class TestAssessmentBundle:
	"""The Assessment agent now has the deterministic permission matrix
	overlaying the live MCP probe — catches role mismatches that span
	multiple customizations in one run."""

	def test_assessment_has_check_permissions_tool(self, mcp_bundles):
		assert "check_permissions_tool" in _names(mcp_bundles["assessment"])

	def test_assessment_keeps_live_check_permission(self, mcp_bundles):
		"""The live MCP probe stays — the matrix is *in addition*, not
		a replacement. Live probe is the source of truth for "this
		exact user, this exact DocType, right now"; the matrix is for
		blanket pre-flight checks across the changeset."""
		assert "check_permission" in _names(mcp_bundles["assessment"])


class TestStubBundle:
	"""``alfred.agents.tool_stubs.TOOL_ASSIGNMENTS`` is the test-only
	fallback used when no MCP client is available. It must mirror the
	production validator depth so tests aren't validating with strictly
	weaker rules than prod."""

	def test_stub_tester_has_real_validators(self):
		from alfred.agents.tool_stubs import TOOL_ASSIGNMENTS
		names = _names(TOOL_ASSIGNMENTS["tester"])
		assert "validate_python_syntax_tool" in names
		assert "validate_doctype_tool" in names
		assert "validate_workflow_tool" in names
		assert "validate_changeset_order_tool" in names

	def test_stub_assessment_has_permission_check(self):
		from alfred.agents.tool_stubs import TOOL_ASSIGNMENTS
		assert "check_permissions_tool" in _names(TOOL_ASSIGNMENTS["assessment"])


class TestInsightsBundleStaysReadOnly:
	"""Read-only insights mode must NOT pick up the new validators —
	they're build-shaped and meaningless for a Q&A agent. This is the
	twin of ``test_insights_crew::test_insights_excludes_local_stubs``
	but covers the new tools too."""

	def test_insights_excludes_validators(self, mcp_bundles):
		names = _names(mcp_bundles["insights"])
		assert "validate_python_syntax_tool" not in names
		assert "validate_js_syntax_tool" not in names
		assert "validate_doctype_tool" not in names
		assert "validate_workflow_tool" not in names
		assert "validate_changeset_order_tool" not in names
		assert "check_permissions_tool" not in names
