"""Load module KB JSON files and cache them in memory.

Mirrors alfred/registry/loader.py (IntentRegistry). Module KBs declare
per-ERPNext-module conventions, validation rules, and detection hints
that module specialists use to reason about domain correctness.

Spec: docs/specs/2026-04-22-module-specialists.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

SCHEMA_DIR = Path(__file__).parent / "modules"


class UnknownModuleError(KeyError):
	"""Raised when a module key is not in the registry."""


class ModuleRegistry:
	_instance: ClassVar["ModuleRegistry | None"] = None

	def __init__(self, kbs: dict[str, dict]):
		self._by_module = kbs
		self._by_target_doctype: dict[str, dict] = {}
		for kb in kbs.values():
			for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
				self._by_target_doctype[dt] = kb

	@classmethod
	def load(cls) -> "ModuleRegistry":
		if cls._instance is not None:
			return cls._instance
		kbs: dict[str, dict] = {}
		for path in SCHEMA_DIR.glob("*.json"):
			if path.name.startswith("_"):
				continue
			data = json.loads(path.read_text())
			kbs[data["module"]] = data
		cls._instance = cls(kbs)
		return cls._instance

	def modules(self) -> list[str]:
		return sorted(self._by_module.keys())

	def get(self, module: str) -> dict:
		if module not in self._by_module:
			raise UnknownModuleError(module)
		return self._by_module[module]

	def for_doctype(self, doctype: str | None) -> dict | None:
		if not doctype:
			return None
		return self._by_target_doctype.get(doctype)

	def detect(
		self, *, prompt: str, target_doctype: str | None,
	) -> tuple[str | None, str | None]:
		"""Heuristic module detection. Returns (module_key, confidence) or (None, None).

		Confidence ladder:
		- "high" when target_doctype matches a declared hint exactly.
		- "medium" when a keyword hint appears in the lowercased prompt.
		- None when neither path matches.
		"""
		if target_doctype:
			kb = self._by_target_doctype.get(target_doctype)
			if kb is not None:
				return kb["module"], "high"

		low = (prompt or "").lower()
		for kb in self._by_module.values():
			for kw in kb.get("detection_hints", {}).get("keyword_hints", []):
				if kw.lower() in low:
					return kb["module"], "medium"

		return None, None
