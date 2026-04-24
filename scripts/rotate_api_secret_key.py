#!/usr/bin/env python3
"""Rotate the processing app's API_SECRET_KEY.

Generates a cryptographically strong 48-byte urlsafe key, writes it to
.env (preserving every other line), backs up the old .env, and prints
the new key plus the two follow-up steps the operator still has to do
by hand (update Alfred Settings on the Frappe site, restart the
processing app).

Safe to run at any time:
  - Creates .env.bak.<timestamp> before touching .env so rollback is
    a single mv away.
  - Never prints the OLD key.
  - Refuses to run if the project root's .env is missing, rather than
    writing a new file from nothing (would mask a real env-loading bug).

Usage:
  $ python scripts/rotate_api_secret_key.py
"""

from __future__ import annotations

import argparse
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

# Resolve .env relative to this script so it works regardless of CWD.
REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
# 48 bytes base64-encoded yields a 64-char string - well above the 32
# min enforced by alfred.config, and still short enough to paste cleanly.
KEY_BYTES = 48


def _generate_key() -> str:
	return secrets.token_urlsafe(KEY_BYTES)


def _read_env(path: Path) -> list[str]:
	return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _replace_or_append(lines: list[str], key: str, value: str) -> list[str]:
	"""Replace the line that sets ``key``; append one if no such line exists.

	Preserves line endings, comments, and blank lines. Matches only at
	the start of the line (after optional whitespace) to avoid
	clobbering a commented-out line like ``# API_SECRET_KEY=old``.
	"""
	prefix = f"{key}="
	out: list[str] = []
	replaced = False
	for line in lines:
		stripped = line.lstrip()
		if not replaced and stripped.startswith(prefix):
			newline = "\n" if line.endswith("\n") else ""
			out.append(f"{key}={value}{newline}")
			replaced = True
		else:
			out.append(line)
	if not replaced:
		if out and not out[-1].endswith("\n"):
			out[-1] += "\n"
		out.append(f"{key}={value}\n")
	return out


def _backup(path: Path) -> Path:
	stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
	bak = path.with_suffix(path.suffix + f".bak.{stamp}")
	shutil.copy2(path, bak)
	return bak


def rotate(env_path: Path = ENV_PATH, *, dry_run: bool = False) -> str:
	"""Rotate API_SECRET_KEY in ``env_path``; return the new key.

	Pure enough to unit-test: only touches disk when ``dry_run`` is False.
	"""
	if not env_path.exists():
		raise FileNotFoundError(
			f"{env_path} does not exist. This script is meant to rotate an "
			"existing secret; create .env (e.g. cp .env.example .env) first."
		)

	new_key = _generate_key()
	lines = _read_env(env_path)
	updated = _replace_or_append(lines, "API_SECRET_KEY", new_key)

	if dry_run:
		return new_key

	bak = _backup(env_path)
	env_path.write_text("".join(updated), encoding="utf-8")
	print(f"Backed up old .env to {bak.name}")
	return new_key


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	parser.add_argument(
		"--dry-run",
		action="store_true",
		help="Print the new key without writing to disk.",
	)
	parser.add_argument(
		"--env-path",
		default=str(ENV_PATH),
		help=f"Path to the .env file (default: {ENV_PATH}).",
	)
	args = parser.parse_args(argv)

	env_path = Path(args.env_path).resolve()
	try:
		new_key = rotate(env_path, dry_run=args.dry_run)
	except FileNotFoundError as e:
		print(f"error: {e}", file=sys.stderr)
		return 2

	print()
	print("New API_SECRET_KEY generated.")
	print()
	print(f"  {new_key}")
	print()
	if args.dry_run:
		print("Dry run - .env was not modified.")
	else:
		print(f"Written to {env_path}.")
	print()
	print("Next steps (manual, required):")
	print("  1. Paste this key into the Frappe site:")
	print("       Desk -> Alfred Settings -> API Key -> <paste> -> Save")
	print("  2. Restart the processing app so it loads the new key.")
	print("  3. Any other client that talks to the processing app must")
	print("     also pick up the new key (see docs/SETUP.md).")
	return 0


if __name__ == "__main__":
	sys.exit(main())
