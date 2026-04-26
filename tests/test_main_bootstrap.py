"""Bootstrap invariants for alfred.main at import time.

alfred.main sets a few os.environ defaults before the first CrewAI or
OTel import, so that a local dev .env that omits them still boots
without phoning telemetry home. These tests guard the setdefault
behaviour against regressions (e.g. someone reordering imports or
deleting the block "because Docker handles it", missing that local
dev doesn't go through Docker).
"""

from __future__ import annotations

import importlib
import os

import pytest

TELEMETRY_OPTOUTS = (
	"CREWAI_DISABLE_TELEMETRY",
	"CREWAI_DISABLE_TRACKING",
	"OTEL_SDK_DISABLED",
)


def test_import_sets_telemetry_optouts():
	# alfred.main may already be imported by a prior test; reimport to exercise
	# the top-level code even when cached.
	import alfred.main
	importlib.reload(alfred.main)

	for key in TELEMETRY_OPTOUTS:
		assert os.environ.get(key) == "true", (
			f"{key} must be 'true' after importing alfred.main; "
			f"got {os.environ.get(key)!r}"
		)


@pytest.mark.parametrize("key", TELEMETRY_OPTOUTS)
def test_operator_override_is_respected(monkeypatch, key):
	# Operator explicitly opts back INTO telemetry. setdefault must NOT
	# clobber an explicit value - that's the whole point of setdefault over
	# assignment.
	monkeypatch.setenv(key, "false")

	import alfred.main
	importlib.reload(alfred.main)

	assert os.environ.get(key) == "false", (
		f"setdefault overwrote an explicit {key}=false; operators lose "
		f"control to toggle telemetry back on for debugging"
	)
