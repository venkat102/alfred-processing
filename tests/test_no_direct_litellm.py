"""Guardrail: no direct litellm imports in our own code.

litellm + httpx + httpcore + anyio has a documented read-timeout hang
when called from a thread-pool executor inside an asyncio event loop.
We replaced every standalone LLM call with urllib via alfred.llm_client.
CrewAI still uses litellm internally (that's fine, it runs synchronously
inside the crew's own thread), but our own code MUST NOT reintroduce the
hang by importing litellm directly.

This test walks the alfred/ tree and fails if any file does. If a new
integration genuinely needs litellm, add its path to _ALLOWED_PATHS and
add a comment explaining why it's safe there.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_BAN_RE = re.compile(r"^\s*(?:import\s+litellm|from\s+litellm\s+import)\b", re.MULTILINE)
_ALFRED_ROOT = pathlib.Path(__file__).resolve().parent.parent / "alfred"

# Paths allowed to import litellm. Currently none - our LLM layer is
# urllib-only and CrewAI imports litellm internally, not via our code.
_ALLOWED_PATHS: set[str] = set()


def _collect_py_files() -> list[pathlib.Path]:
	return [p for p in _ALFRED_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_direct_litellm_imports_in_alfred_package():
	violations = []
	for path in _collect_py_files():
		rel = str(path.relative_to(_ALFRED_ROOT.parent))
		if rel in _ALLOWED_PATHS:
			continue
		content = path.read_text(encoding="utf-8", errors="replace")
		if _BAN_RE.search(content):
			violations.append(rel)

	if violations:
		pytest.fail(
			"Direct `import litellm` found in:\n  "
			+ "\n  ".join(violations)
			+ "\n\nOur standalone LLM calls MUST go through alfred.llm_client "
			+ "(urllib-based). litellm's httpx+anyio path hangs under a "
			+ "thread-pool-in-asyncio executor. If you genuinely need litellm "
			+ "in a new integration, add its path to _ALLOWED_PATHS with a "
			+ "comment explaining why it's safe there."
		)
