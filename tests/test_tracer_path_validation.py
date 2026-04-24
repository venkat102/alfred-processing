"""Tests for tracer.ALFRED_TRACE_PATH validation.

Defence-in-depth against an attacker setting ALFRED_TRACE_PATH to a
sensitive location (container env injection, CI secret leakage). The
_safe_trace_path helper must accept typical operator inputs (relative
default, ~-prefixed home, /tmp, /var/tmp) and reject traversal (..)
and absolute paths outside the whitelist.
"""

from __future__ import annotations

import os
import tempfile

from alfred.obs.tracer import _DEFAULT_TRACE_PATH, _safe_trace_path


def test_default_relative_path_is_accepted():
	# Resolves to realpath under CWD. Not equal to the raw input, but
	# still anchored inside cwd.
	resolved = _safe_trace_path("alfred_trace.jsonl")
	assert resolved.startswith(os.path.realpath(os.getcwd()))


def test_home_tilde_is_expanded_and_accepted():
	resolved = _safe_trace_path("~/alfred_trace.jsonl")
	assert resolved.startswith(os.path.realpath(os.path.expanduser("~")))


def test_tmp_absolute_is_accepted():
	# macOS resolves /tmp -> /private/tmp; the whitelist includes both via
	# explicit realpath() of /tmp.
	resolved = _safe_trace_path("/tmp/alfred_trace.jsonl")
	assert resolved.endswith("alfred_trace.jsonl")
	assert os.path.isabs(resolved)
	# Must not have fallen back to the default
	assert resolved != _DEFAULT_TRACE_PATH


def test_var_tmp_absolute_is_accepted():
	resolved = _safe_trace_path("/var/tmp/alfred_trace.jsonl")
	assert resolved.endswith("alfred_trace.jsonl")
	assert os.path.isabs(resolved)
	assert resolved != _DEFAULT_TRACE_PATH


def test_tempfile_dir_is_accepted():
	target = os.path.join(tempfile.gettempdir(), "alfred_trace.jsonl")
	resolved = _safe_trace_path(target)
	assert resolved.endswith("alfred_trace.jsonl")
	assert resolved != _DEFAULT_TRACE_PATH


def test_etc_absolute_is_rejected():
	# Classic sensitive target - must fall back to default.
	resolved = _safe_trace_path("/etc/passwd")
	assert resolved == _DEFAULT_TRACE_PATH


def test_dotdot_traversal_is_rejected():
	resolved = _safe_trace_path("../../../etc/passwd")
	assert resolved == _DEFAULT_TRACE_PATH


def test_dotdot_mid_path_is_rejected():
	# Attacker embeds '..' in an otherwise-plausible path.
	resolved = _safe_trace_path("logs/../../../etc/service.conf")
	assert resolved == _DEFAULT_TRACE_PATH


def test_symlink_escape_is_rejected(tmp_path):
	# If an attacker creates a symlink under a whitelisted dir that
	# points to /etc, realpath() follows the symlink and the escape is
	# caught by the whitelist check.
	target_outside = "/etc/passwd"
	symlink = tmp_path / "evil.jsonl"
	try:
		os.symlink(target_outside, symlink)
	except (OSError, PermissionError):
		# Some CI environments disallow symlink creation; skip.
		import pytest
		pytest.skip("cannot create symlink in this environment")
	resolved = _safe_trace_path(str(symlink))
	assert resolved == _DEFAULT_TRACE_PATH


def test_root_absolute_rejected():
	# Not under CWD, not under HOME, not a temp dir.
	resolved = _safe_trace_path("/root/.bashrc")
	assert resolved == _DEFAULT_TRACE_PATH
