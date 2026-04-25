"""Tests for scripts/rotate_api_secret_key.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rotate_api_secret_key import _replace_or_append, rotate


class TestReplaceOrAppend:
	def test_replaces_existing_line(self):
		lines = ["HOST=0.0.0.0\n", "API_SECRET_KEY=old\n", "PORT=8000\n"]
		out = _replace_or_append(lines, "API_SECRET_KEY", "new")
		assert out[1] == "API_SECRET_KEY=new\n"
		assert out[0] == "HOST=0.0.0.0\n"
		assert out[2] == "PORT=8000\n"

	def test_appends_when_missing(self):
		lines = ["HOST=0.0.0.0\n"]
		out = _replace_or_append(lines, "API_SECRET_KEY", "new")
		assert out[-1] == "API_SECRET_KEY=new\n"

	def test_ignores_commented_out_line(self):
		lines = ["# API_SECRET_KEY=old\n", "HOST=x\n"]
		out = _replace_or_append(lines, "API_SECRET_KEY", "new")
		# Commented line preserved; new value appended
		assert out[0] == "# API_SECRET_KEY=old\n"
		assert "API_SECRET_KEY=new\n" in out[1:]

	def test_preserves_no_trailing_newline(self):
		# Last line has no trailing newline; we add one before appending
		lines = ["HOST=x"]
		out = _replace_or_append(lines, "API_SECRET_KEY", "new")
		assert out[-1] == "API_SECRET_KEY=new\n"


class TestRotate:
	def test_rotates_existing_key_and_backs_up(self, tmp_path: Path):
		env = tmp_path / ".env"
		env.write_text("HOST=0.0.0.0\nAPI_SECRET_KEY=oldkey\nREDIS_URL=redis://x\n")
		new_key = rotate(env)
		content = env.read_text()
		assert f"API_SECRET_KEY={new_key}" in content
		assert "API_SECRET_KEY=oldkey" not in content
		# Other lines untouched
		assert "HOST=0.0.0.0" in content
		assert "REDIS_URL=redis://x" in content
		# Backup exists and contains the old key
		backups = list(tmp_path.glob(".env.bak.*"))
		assert len(backups) == 1
		assert "API_SECRET_KEY=oldkey" in backups[0].read_text()

	def test_appends_key_when_absent(self, tmp_path: Path):
		env = tmp_path / ".env"
		env.write_text("HOST=0.0.0.0\n")
		new_key = rotate(env)
		assert f"API_SECRET_KEY={new_key}" in env.read_text()

	def test_generates_strong_key(self, tmp_path: Path):
		env = tmp_path / ".env"
		env.write_text("API_SECRET_KEY=placeholder\n")
		new_key = rotate(env)
		# 48 bytes base64-encoded yields a 64-char string
		assert len(new_key) >= 32

	def test_raises_when_env_missing(self, tmp_path: Path):
		with pytest.raises(FileNotFoundError):
			rotate(tmp_path / "no-such.env")

	def test_dry_run_does_not_write(self, tmp_path: Path):
		env = tmp_path / ".env"
		original = "API_SECRET_KEY=kept\n"
		env.write_text(original)
		new_key = rotate(env, dry_run=True)
		assert new_key  # a key is still generated
		assert env.read_text() == original
		assert list(tmp_path.glob(".env.bak.*")) == []
