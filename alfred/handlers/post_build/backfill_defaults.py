"""Fill missing registry fields on a Changeset before it reaches the client.

Safety net: if the per-intent Builder specialist's LLM output drops a
shape-defining field (e.g. forgets ``autoname``), this post-processor
inserts the registry default and annotates ``field_defaults_meta`` so the
client can still render it as a defaulted row. User-provided values are
never overwritten.

Spec: ``docs/specs/2026-04-21-doctype-builder-specialist.md``
"""

from __future__ import annotations

import copy
import logging

from alfred.models.agent_outputs import Changeset, ChangesetItem, FieldMeta
from alfred.registry.loader import IntentRegistry, UnknownIntentError
from alfred.registry.module_loader import ModuleRegistry, UnknownModuleError

logger = logging.getLogger("alfred.handlers.post_build.backfill")


def _schema_for(intent: str | None, doctype: str | None) -> dict | None:
	"""Resolve which intent registry to apply to a change.

	When ``intent`` is provided, backfill is gated on the classified
	intent: only items whose ``doctype`` equals the intent's target
	doctype are backfilled. This prevents, for example, a ``create_doctype``
	intent that happens to emit a Custom Field row from getting that row
	filled with the 22 ``create_custom_field`` defaults.

	When ``intent`` is None (no classification context available), fall
	back to legacy behaviour: look up the intent by ``doctype`` alone.
	"""
	registry = IntentRegistry.load()
	if intent:
		try:
			schema = registry.get(intent)
		except UnknownIntentError:
			return None
		if doctype and schema.get("doctype") == doctype:
			return schema
		return None
	return registry.for_doctype(doctype) if doctype else None


def backfill_defaults(
	changeset: Changeset, *, intent: str | None = None,
) -> Changeset:
	"""Return a new Changeset with registry fields backfilled and annotated.

	Typed entry point. Pipeline code that works with raw dicts (as produced
	by ``_extract_changes``) should call :func:`backfill_defaults_raw` instead.

	When ``intent`` is supplied, only items whose doctype matches that
	intent's target doctype receive backfill. Callers without an intent
	classification (legacy tests, ad-hoc usage) omit it and get the
	legacy by-doctype lookup.
	"""
	new_items: list[ChangesetItem] = []
	for item in changeset.items:
		schema = _schema_for(intent, item.doctype)
		if schema is None:
			new_items.append(item)
			continue
		new_items.append(_backfill_item(item, schema))
	return Changeset(items=new_items)


def backfill_defaults_raw(
	changes: list[dict],
	*,
	intent: str | None = None,
	module: str | None = None,
	secondary_modules: list[str] | None = None,
) -> list[dict]:
	"""Raw-dict variant used by the pipeline.

	V1 behaviour (``module`` is None): fills missing intent-registry
	fields in ``data`` and appends a ``field_defaults_meta`` annotation.

	V2 behaviour (``module`` set): after V1 pass, layers the primary
	module KB's conventions on top - adds any missing
	``permissions_add_roles`` entries and, if the V1 pass defaulted
	``autoname``, swaps in the first entry from the module's
	``naming_patterns`` with a module-aware rationale.

	V3 behaviour (``secondary_modules`` also set): each secondary
	module's ``permissions_add_roles`` are appended too, deduped against
	the primary's rows. Primary's naming pattern always wins - secondary
	modules never override naming.

	Intent gating: when ``intent`` is provided, only items whose doctype
	matches the intent's target doctype are backfilled; all other items
	pass through untouched. When ``intent`` is None, legacy by-doctype
	lookup applies.

	Unknown intent / module keys are skipped silently.
	"""
	out: list[dict] = []
	for change in changes:
		doctype = change.get("doctype")
		schema = _schema_for(intent, doctype)
		if schema is None:
			out.append(change)
			continue
		backfilled = _backfill_raw(change, schema)
		if module:
			backfilled = _apply_module_defaults(backfilled, module)
			for sec in secondary_modules or []:
				backfilled = _apply_secondary_module_defaults(backfilled, sec)
		out.append(backfilled)
	return out


def _apply_secondary_module_defaults(change: dict, module: str) -> dict:
	"""Secondary-module defaults: permission rows only; no naming swap.

	Primary module owns naming. Secondary contributes additional
	permission rows deduped against whatever is already present.
	Rationale tag flags these as secondary-context additions so the UI
	can surface them differently.
	"""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return change

	display_name = kb.get("display_name", module)
	conv = kb.get("conventions", {})
	new = {**change}
	data = copy.deepcopy(new.get("data") or {})
	meta = copy.deepcopy(new.get("field_defaults_meta") or {})

	existing_perms = data.get("permissions") or []
	existing_roles = {p.get("role") for p in existing_perms if isinstance(p, dict)}
	appended: list[str] = []
	for row in conv.get("permissions_add_roles", []):
		if row.get("role") and row["role"] not in existing_roles:
			existing_perms.append(copy.deepcopy(row))
			existing_roles.add(row["role"])
			appended.append(row["role"])
	if appended:
		data["permissions"] = existing_perms
		prev = meta.get("permissions", {})
		prev_rationale = prev.get("rationale", "")
		addl = (
			f"Added {', '.join(appended)} because request touches "
			f"{display_name} as secondary context."
		)
		meta["permissions"] = {
			"source": "default",
			"rationale": (prev_rationale + " " + addl).strip() if prev_rationale else addl,
		}
	new["data"] = data
	new["field_defaults_meta"] = meta
	return new


def _apply_module_defaults(change: dict, module: str) -> dict:
	"""Layer a module KB's conventions on top of an intent-backfilled change."""
	try:
		kb = ModuleRegistry.load().get(module)
	except UnknownModuleError:
		return change

	display_name = kb.get("display_name", module)
	conv = kb.get("conventions", {})
	new = {**change}
	data = copy.deepcopy(new.get("data") or {})
	meta = copy.deepcopy(new.get("field_defaults_meta") or {})

	# 1. Permissions: add any module roles that aren't already present.
	existing_perms = data.get("permissions") or []
	existing_roles = {p.get("role") for p in existing_perms if isinstance(p, dict)}
	appended: list[str] = []
	for row in conv.get("permissions_add_roles", []):
		if row.get("role") and row["role"] not in existing_roles:
			existing_perms.append(copy.deepcopy(row))
			existing_roles.add(row["role"])
			appended.append(row["role"])
	if appended:
		data["permissions"] = existing_perms
		prev = meta.get("permissions", {})
		prev_rationale = prev.get("rationale", "")
		module_reason = f"Added {', '.join(appended)} because target module is {display_name}."
		meta["permissions"] = {
			"source": "default",
			"rationale": (prev_rationale + " " + module_reason).strip() if prev_rationale else module_reason,
		}

	# 2. Autoname swap: only if intent backfill defaulted it.
	auto_meta = meta.get("autoname") or {}
	naming_patterns = conv.get("naming_patterns") or []
	if auto_meta.get("source") == "default" and naming_patterns:
		data["autoname"] = naming_patterns[0]
		meta["autoname"] = {
			"source": "default",
			"rationale": (
				f"Module {display_name} conventionally uses {naming_patterns[0]}."
			),
		}

	new["data"] = data
	new["field_defaults_meta"] = meta
	return new


def _backfill_raw(change: dict, schema: dict) -> dict:
	data = copy.deepcopy(change.get("data") or {})
	meta = copy.deepcopy(change.get("field_defaults_meta") or {})

	for field in schema["fields"]:
		key = field["key"]
		present = key in data and data[key] not in (None, "")

		if present:
			if key not in meta:
				meta[key] = {"source": "user"}
			continue

		if "default" not in field:
			if key not in meta:
				meta[key] = {"source": "user"}
			continue

		data[key] = copy.deepcopy(field["default"])
		if key not in meta:
			entry = {"source": "default"}
			rationale = field.get("rationale")
			if rationale:
				entry["rationale"] = rationale
			meta[key] = entry

	new = dict(change)
	new["data"] = data
	new["field_defaults_meta"] = meta
	return new


def _backfill_item(item: ChangesetItem, schema: dict) -> ChangesetItem:
	data = copy.deepcopy(item.data)
	meta: dict[str, FieldMeta] = dict(item.field_defaults_meta or {})

	for field in schema["fields"]:
		key = field["key"]
		present = key in data and data[key] not in (None, "")

		if present:
			if key not in meta:
				meta[key] = FieldMeta(source="user")
			continue

		if "default" not in field:
			# Required field with no default; leave absent/empty and flag as user-sourced
			# so the client renders it as a required-empty input.
			if key not in meta:
				meta[key] = FieldMeta(source="user")
			continue

		data[key] = copy.deepcopy(field["default"])
		if key not in meta:
			meta[key] = FieldMeta(source="default", rationale=field.get("rationale"))

	return ChangesetItem(
		operation=item.operation,
		doctype=item.doctype,
		data=data,
		field_defaults_meta=meta,
	)
