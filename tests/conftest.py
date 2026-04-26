"""Test-suite bootstrap. Runs before any test module imports.

Sets a strong API_SECRET_KEY into the process environment so pytest
collection doesn't crash when the repo's .env carries a weak
placeholder (common in local dev). Individual tests can still override
via their own fixtures; this is only the collection-phase floor.

Why this is at conftest.py rather than inside each test module: pytest
collection imports the test modules, and several of them transitively
import alfred.main, which calls create_app() at module level, which
calls get_settings(). Any validator-tripping key in .env would fail
collection before a test-module-level fixture gets a chance to run.
"""

from __future__ import annotations

import os

# 48 chars, well above the 32-byte validator floor. Do NOT overwrite if
# the operator has already exported a strong key - respect explicit env.
os.environ.setdefault(
	"API_SECRET_KEY",
	"test-a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4",
)
