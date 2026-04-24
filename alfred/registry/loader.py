"""Load intent schema JSON files and cache them in memory.

The registry is a set of JSON files under ``alfred/registry/intents/`` that
declare, per intent, the shape-defining fields a Builder specialist must
populate in a ``ChangesetItem``'s ``data`` dict. See the spec at
``docs/specs/2026-04-21-doctype-builder-specialist.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

SCHEMA_DIR = Path(__file__).parent / "intents"


class UnknownIntentError(KeyError):
	"""Raised when an intent key is not in the registry."""


class IntentRegistry:
	_instance: ClassVar[IntentRegistry | None] = None

	def __init__(self, schemas: dict[str, dict]):
		self._by_intent = schemas
		self._by_doctype = {s["doctype"]: s for s in schemas.values()}

	@classmethod
	def load(cls) -> IntentRegistry:
		if cls._instance is not None:
			return cls._instance
		schemas: dict[str, dict] = {}
		for path in SCHEMA_DIR.glob("*.json"):
			if path.name.startswith("_"):
				continue
			data = json.loads(path.read_text())
			schemas[data["intent"]] = data
		cls._instance = cls(schemas)
		return cls._instance

	def intents(self) -> list[str]:
		return sorted(self._by_intent.keys())

	def get(self, intent: str) -> dict:
		if intent not in self._by_intent:
			raise UnknownIntentError(intent)
		return self._by_intent[intent]

	def for_doctype(self, doctype: str) -> dict | None:
		"""Look up the registry entry whose ``doctype`` field matches.

		Used by the backfill post-processor to find the right intent for a
		``ChangesetItem`` after the crew has already produced it.
		"""
		return self._by_doctype.get(doctype)
