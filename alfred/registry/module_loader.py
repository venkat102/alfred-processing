"""Load module KB JSON files and cache them in memory.

Mirrors alfred/registry/loader.py (IntentRegistry). Module KBs declare
per-ERPNext-module conventions, validation rules, and detection hints
that module specialists use to reason about domain correctness.

Families (added 2026-04-23) group related modules together and carry
cross-module invariants shared by their members. Every non-custom
module JSON declares a ``family`` field pointing to one of the family
KBs under ``modules/_families/``. The module specialist uses the
family KB to prepend a FAMILY CONTEXT section above the per-module
snippet so Frappe intent specialists see both layers.

Spec: docs/specs/2026-04-22-module-specialists.md.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import ClassVar

SCHEMA_DIR = Path(__file__).parent / "modules"
FAMILIES_DIR = SCHEMA_DIR / "_families"


class UnknownModuleError(KeyError):
	"""Raised when a module key is not in the registry."""


class UnknownFamilyError(KeyError):
	"""Raised when a family key is not in the registry."""


class ModuleRegistry:
	_instance: ClassVar[ModuleRegistry | None] = None

	def __init__(self, kbs: dict[str, dict], families: dict[str, dict]):
		self._by_module = kbs
		self._by_family = families
		self._by_target_doctype: dict[str, dict] = {}
		for kb in kbs.values():
			for dt in kb.get("detection_hints", {}).get("target_doctype_matches", []):
				self._by_target_doctype[dt] = kb

	@classmethod
	def load(cls) -> ModuleRegistry:
		if cls._instance is not None:
			return cls._instance
		kbs: dict[str, dict] = {}
		# Sort by filename for deterministic iteration order. Path.glob
		# returns filesystem-insertion order (not alphabetical) which
		# makes detection tie-breaking non-deterministic on different
		# systems or after re-saves. Sorting fixes the order so tests
		# and prod agree on which module wins when multiple keywords hit.
		for path in sorted(SCHEMA_DIR.glob("*.json"), key=lambda p: p.name):
			if path.name.startswith("_"):
				continue
			data = json.loads(path.read_text())
			kbs[data["module"]] = data

		families: dict[str, dict] = {}
		if FAMILIES_DIR.is_dir():
			for path in sorted(FAMILIES_DIR.glob("*.json"), key=lambda p: p.name):
				if path.name.startswith("_"):
					continue
				data = json.loads(path.read_text())
				families[data["name"]] = data

		cls._instance = cls(kbs, families)
		return cls._instance

	def modules(self) -> list[str]:
		return sorted(self._by_module.keys())

	def get(self, module: str) -> dict:
		if module not in self._by_module:
			raise UnknownModuleError(module)
		return self._by_module[module]

	def families(self) -> list[str]:
		return sorted(self._by_family.keys())

	def get_family(self, family: str) -> dict:
		if family not in self._by_family:
			raise UnknownFamilyError(family)
		return self._by_family[family]

	def family_for_module(self, module: str) -> str | None:
		"""Return the family name for a module, or None if the module has no family.

		``custom`` is intentionally familyless - it's the catch-all KB
		for user-defined DocTypes outside canonical ERPNext modules.
		"""
		kb = self._by_module.get(module)
		if kb is None:
			return None
		return kb.get("family")

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
				# Word-boundary match so "account" doesn't hit "accountant"
				# or "accounts receivable agent". Multi-word phrases and
				# hyphenated keywords work naturally via \b on each edge.
				if re.search(rf"\b{re.escape(kw.strip().lower())}\b", low):
					return kb["module"], "medium"

		return None, None

	def detect_all(
		self, *, prompt: str, target_doctype: str | None, max_secondaries: int = 2,
	) -> tuple[str | None, str, list[str]]:
		"""Return (primary, confidence, secondaries).

		Primary is chosen by target_doctype match (high confidence) or
		first keyword match (medium confidence). Additional keyword
		matches past the primary become secondaries, up to
		max_secondaries, deduped against the primary.

		Used by the V3 multi-module pipeline. The V2 single-module
		``detect()`` remains unchanged for back-compat.
		"""
		primary: str | None = None
		confidence: str = ""

		if target_doctype:
			kb = self._by_target_doctype.get(target_doctype)
			if kb is not None:
				primary = kb["module"]
				confidence = "high"

		low = (prompt or "").lower()
		keyword_hits: list[str] = []
		for kb in self._by_module.values():
			for kw in kb.get("detection_hints", {}).get("keyword_hints", []):
				if re.search(rf"\b{re.escape(kw.strip().lower())}\b", low):
					if kb["module"] not in keyword_hits:
						keyword_hits.append(kb["module"])
					break

		if primary is None and keyword_hits:
			primary = keyword_hits[0]
			confidence = "medium"

		secondaries = [m for m in keyword_hits if m != primary][:max_secondaries]
		return primary, confidence, secondaries
