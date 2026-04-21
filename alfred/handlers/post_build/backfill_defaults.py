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
from alfred.registry.loader import IntentRegistry

logger = logging.getLogger("alfred.handlers.post_build.backfill")


def backfill_defaults(changeset: Changeset) -> Changeset:
	"""Return a new Changeset with registry fields backfilled and annotated."""
	registry = IntentRegistry.load()
	new_items: list[ChangesetItem] = []
	for item in changeset.items:
		schema = registry.for_doctype(item.doctype)
		if schema is None:
			new_items.append(item)
			continue
		new_items.append(_backfill_item(item, schema))
	return Changeset(items=new_items)


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
