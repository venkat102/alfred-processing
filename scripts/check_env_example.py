#!/usr/bin/env python3
"""Enforce that every Settings field appears in .env.example.

Problem this solves (TD-M10): operators deploy, Settings declares a
field they haven't set, the app boots with a silent default, and nobody
notices the config gap until a feature mis-behaves. This check blocks
PRs that add a Settings field without updating .env.example.

What counts as "documented": any line in .env.example matching
  ``^(?:#\\s*)?FIELD_NAME=``
so both `FOO=bar` and `# FOO=` count. Blank-value lines (``FOO=``)
also count — they're explicit placeholders.

What is NOT flagged: keys in .env.example that aren't in Settings.
Third-party vars (CREWAI_DISABLE_TELEMETRY, OTEL_SDK_DISABLED, etc.)
legitimately live in .env.example for operator awareness without
being modeled by our Settings.

Usage:
  ./scripts/check_env_example.py                 # uses defaults
  ./scripts/check_env_example.py path/to/.env.example

Exit codes:
  0  every Settings field has a line in .env.example
  1  one or more fields are undocumented (list printed to stderr)
  2  Settings failed to import, or .env.example missing
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


# Lines we skip: blank, pure comment without KEY=, section headers.
_KEY_LINE_RE = re.compile(r"^\s*(?:#\s*)?([A-Z_][A-Z0-9_]*)\s*=")


def extract_keys(env_example_path: Path) -> set[str]:
	"""Return the set of env-var names mentioned in the file.

	Catches both uncommented and commented-template forms.
	"""
	if not env_example_path.is_file():
		print(
			f"error: env example not found at {env_example_path}",
			file=sys.stderr,
		)
		sys.exit(2)
	keys: set[str] = set()
	for raw_line in env_example_path.read_text().splitlines():
		m = _KEY_LINE_RE.match(raw_line)
		if m:
			keys.add(m.group(1))
	return keys


def settings_fields() -> set[str]:
	"""Return the set of field names declared on alfred.config.Settings.

	Runs inside a try so an import failure produces a clear error
	(exit 2) rather than a stack trace.
	"""
	try:
		from alfred.config import Settings
	except Exception as e:
		print(f"error: failed to import Settings: {e}", file=sys.stderr)
		sys.exit(2)
	return set(Settings.model_fields.keys())


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument(
		"env_example",
		nargs="?",
		default=".env.example",
		help="Path to .env.example (default: .env.example in cwd)",
	)
	args = parser.parse_args()

	env_path = Path(args.env_example)
	declared_fields = settings_fields()
	documented_keys = extract_keys(env_path)

	missing = sorted(declared_fields - documented_keys)
	if missing:
		print(
			f"error: {len(missing)} Settings field(s) missing from {env_path}:",
			file=sys.stderr,
		)
		for name in missing:
			print(f"  - {name}", file=sys.stderr)
		print(
			"\nFix: add a line to .env.example for each missing field. "
			"Commented templates (`# FIELD=`) are accepted for optional "
			"fields; uncommented lines document required ones.",
			file=sys.stderr,
		)
		return 1

	print(
		f"ok: all {len(declared_fields)} Settings field(s) documented in "
		f"{env_path}",
	)
	return 0


if __name__ == "__main__":
	sys.exit(main())
