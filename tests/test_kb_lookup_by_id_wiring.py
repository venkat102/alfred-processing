"""Pin the L3 wire: ``lookup_kb_entry_by_id`` lifts ``fkb.lookup_entry``
into a CrewAI tool the Developer agent can call when it already knows
an entry id (e.g. forwarded from a previous ``lookup_frappe_knowledge``
result).

Before the wire, ``alfred/knowledge/fkb.py::lookup_entry`` had no
production callers. The audit's L3 noted this; the resolution was
to wire (not delete) since direct-by-id lookup is a legitimate (if
infrequent) use case."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest


def _names(tools) -> set[str]:
	return {getattr(t, "name", "") for t in tools}


@pytest.fixture
def mcp_bundles():
	from alfred.tools.mcp_tools import build_mcp_tools
	return build_mcp_tools(MagicMock())


def test_developer_bundle_has_lookup_kb_entry_by_id(mcp_bundles):
	"""The Developer agent gets the by-id lookup so it can refetch a
	full KB entry without re-running keyword search."""
	assert "lookup_kb_entry_by_id" in _names(mcp_bundles["developer"])


def test_lookup_returns_full_entry_when_id_exists():
	"""Happy path: the wrapper hands the id straight through to
	``fkb.lookup_entry`` and JSON-encodes the result for the agent."""
	from alfred.tools import mcp_tools

	# Find the underlying function CrewAI's @tool decorator wraps.
	tool_obj = mcp_tools.lookup_kb_entry_by_id
	inner = getattr(tool_obj, "func", tool_obj)

	fake_entry = {
		"id": "server_script_no_imports",
		"kind": "rule",
		"title": "Don't import os/sys in server scripts",
		"summary": "Server Scripts run sandboxed; imports are blocked.",
		"keywords": ["server script", "import"],
		"body": "...",
		"verified_on": "2026-01-01",
	}

	with patch("alfred.knowledge.fkb.lookup_entry", return_value=fake_entry):
		raw = inner.run("server_script_no_imports") if hasattr(inner, "run") else inner("server_script_no_imports")

	result = json.loads(raw)
	assert result["found"] is True
	assert result["id"] == "server_script_no_imports"
	assert result["title"] == "Don't import os/sys in server scripts"


def test_lookup_returns_found_false_for_missing_id():
	"""The agent must be able to tell "this entry doesn't exist" apart
	from "the entry exists but has no body" — the explicit ``found``
	flag covers that."""
	from alfred.tools import mcp_tools

	tool_obj = mcp_tools.lookup_kb_entry_by_id
	inner = getattr(tool_obj, "func", tool_obj)

	with patch("alfred.knowledge.fkb.lookup_entry", return_value=None):
		raw = inner.run("nonexistent_id_xyz") if hasattr(inner, "run") else inner("nonexistent_id_xyz")

	result = json.loads(raw)
	assert result["found"] is False
	assert result["id"] == "nonexistent_id_xyz"
