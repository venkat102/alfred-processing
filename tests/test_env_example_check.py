"""Tests for scripts/check_env_example.py (TD-M10).

Verifies the parser accepts both uncommented and commented-template
forms, detects missing fields, and ignores extras. Also runs the
script against the REAL .env.example to guard against future drift
landing in the codebase — the CI job runs the same script, so a
passing local test is an early warning.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_env_example.py"


def _run(env_path: Path) -> subprocess.CompletedProcess:
	return subprocess.run(
		[sys.executable, str(SCRIPT), str(env_path)],
		capture_output=True,
		text=True,
		cwd=REPO_ROOT,
	)


# ── Parser unit tests ─────────────────────────────────────────────


def test_extract_keys_uncommented():
	from scripts.check_env_example import extract_keys
	env = REPO_ROOT / "tests" / "_tmp_env_uncommented"
	env.write_text("FOO=bar\nBAZ=qux\n")
	try:
		keys = extract_keys(env)
	finally:
		env.unlink()
	assert keys == {"FOO", "BAZ"}


def test_extract_keys_commented_template():
	from scripts.check_env_example import extract_keys
	env = REPO_ROOT / "tests" / "_tmp_env_commented"
	env.write_text("# FOO=\n# BAZ=default-value\n")
	try:
		keys = extract_keys(env)
	finally:
		env.unlink()
	assert keys == {"FOO", "BAZ"}


def test_extract_keys_mixed_and_comments():
	from scripts.check_env_example import extract_keys
	env = REPO_ROOT / "tests" / "_tmp_env_mixed"
	env.write_text(
		"# Section header\n"
		"\n"
		"FOO=bar\n"
		"# BAZ=\n"
		"# section note with no key at all\n"
		"QUX=\n"
	)
	try:
		keys = extract_keys(env)
	finally:
		env.unlink()
	assert keys == {"FOO", "BAZ", "QUX"}


def test_extract_keys_ignores_lowercase():
	# Env var convention is UPPER_SNAKE; the regex requires it. A
	# stray `host=...` line should not be treated as a key.
	from scripts.check_env_example import extract_keys
	env = REPO_ROOT / "tests" / "_tmp_env_case"
	env.write_text("host=example.com\nHOST=example.com\n")
	try:
		keys = extract_keys(env)
	finally:
		env.unlink()
	assert keys == {"HOST"}


# ── End-to-end via subprocess ─────────────────────────────────────


def test_script_passes_against_real_env_example():
	# Guard: if someone adds a Settings field without updating
	# .env.example, this test fails locally AND in CI. Early warning
	# is the whole point of TD-M10.
	env = REPO_ROOT / ".env.example"
	result = _run(env)
	assert result.returncode == 0, (
		f"check_env_example failed against real file:\n"
		f"  stdout={result.stdout!r}\n"
		f"  stderr={result.stderr!r}"
	)
	assert "all" in result.stdout.lower()


def test_script_fails_on_missing_field(tmp_path):
	# Create a broken .env.example missing most fields.
	broken = tmp_path / "bad.env"
	broken.write_text("# just a comment, no keys\n")
	result = _run(broken)
	assert result.returncode == 1, (
		f"expected exit 1 when fields missing; got {result.returncode}\n"
		f"stdout={result.stdout!r} stderr={result.stderr!r}"
	)
	# Must mention at least one known required field.
	assert "API_SECRET_KEY" in result.stderr


def test_script_fails_on_nonexistent_file(tmp_path):
	result = _run(tmp_path / "does_not_exist.env")
	assert result.returncode == 2
	assert "not found" in result.stderr


def test_script_ignores_extra_keys(tmp_path):
	# If .env.example has keys that aren't Settings fields (third-party
	# vars), the script should NOT complain — only missing Settings
	# fields are errors.
	env = tmp_path / "extras.env"
	# Build a .env.example with every real Settings field PLUS extras.
	from alfred.config import Settings

	lines = [f"# {name}=" for name in Settings.model_fields]
	lines.append("CREWAI_DISABLE_TELEMETRY=true")
	lines.append("THIRD_PARTY_VAR=xyz")
	env.write_text("\n".join(lines) + "\n")
	result = _run(env)
	assert result.returncode == 0, (
		f"script flagged third-party keys incorrectly:\n"
		f"stdout={result.stdout!r} stderr={result.stderr!r}"
	)
