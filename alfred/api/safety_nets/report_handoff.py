"""Insights-to-Report handoff safety nets (TD-H1 extraction).

Two related concerns that share the same per-item iteration:

  report_name (V4): the Insights handoff carries ``suggested_name``.
    If the specialist omitted ``data.report_name`` (Frappe requires
    it; Report autoname is ``field:report_name``), derive from the
    handoff so the dry-run doesn't fail on a missing required field.

  aggregation (TD-M7 / TD-M8): aggregation prompts (``top N <X> by
    <metric>``) need Query Report with a ready-made SQL body. The
    extractor already built one; force ``report_type``,
    ``ref_doctype``, and ``query`` to match the handoff because the
    specialist has proven unreliable at emitting GROUP BY SQL. Also
    forwards the candidate's filter defaults into ``filters_json``
    so Frappe opens the Query Report with the right date range
    pre-filled.

Both are per-item mutations on ``ctx.changes`` plus parallel
annotations in ``item["field_defaults_meta"]`` so the preview UI
can render "default" pills with a rationale.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.api.pipeline import PipelineContext

logger = logging.getLogger("alfred.safety_nets.report_handoff")


def apply_report_handoff_safety_net(ctx: PipelineContext) -> None:
	"""Backfill Report items from the Insights-to-Report handoff.

	No-op unless ``ctx.intent == "create_report"``, ``ctx.changes`` is
	non-empty, and ``ctx.report_candidate`` is a dict. Mutates ``ctx.
	changes`` in place.
	"""
	if not (
		ctx.changes
		and ctx.intent == "create_report"
		and isinstance(ctx.report_candidate, dict)
	):
		return

	candidate = ctx.report_candidate
	suggested_name = candidate.get("suggested_name")
	cand_query = candidate.get("query")
	cand_target = candidate.get("target_doctype")
	cand_aggregation = candidate.get("aggregation")
	cand_filters = candidate.get("filters") or []

	for item in ctx.changes:
		if item.get("doctype") != "Report":
			continue
		data = item.setdefault("data", {})
		meta = item.setdefault("field_defaults_meta", {})

		if suggested_name and not data.get("report_name"):
			data["report_name"] = suggested_name
			meta["report_name"] = {
				"source": "default",
				"rationale": (
					"Filled from the Insights-to-Report handoff's "
					"suggested_name because the specialist's output "
					"omitted it. Edit before deploy if you want a "
					"different report title."
				),
			}

		# Aggregation-shape handoff: force the three aggregation-
		# critical fields because the specialist has proven unreliable
		# at emitting GROUP BY SQL; the handoff's pre-rendered query
		# is authoritative.
		if cand_aggregation and cand_query:
			if data.get("report_type") != "Query Report":
				data["report_type"] = "Query Report"
				meta["report_type"] = {
					"source": "default",
					"rationale": (
						"Aggregation prompt (top N by <metric>) requires "
						"Query Report; Report Builder cannot express "
						"GROUP BY + SUM. Forced by handoff safety net."
					),
				}
			if data.get("query") != cand_query:
				data["query"] = cand_query
				meta["query"] = {
					"source": "default",
					"rationale": (
						"Copied verbatim from the Insights-to-Report "
						"handoff's pre-rendered aggregation SQL "
						"(authoritative - specialist-emitted SQL for "
						"GROUP BY aggregations has proven unreliable). "
						"Edit before deploy if the date range or metric "
						"needs adjusting."
					),
				}
			if cand_target and data.get("ref_doctype") != cand_target:
				data["ref_doctype"] = cand_target
				meta["ref_doctype"] = {
					"source": "default",
					"rationale": (
						"Set to the metric's source DocType (e.g. Sales "
						"Invoice for revenue) since the aggregation lives "
						"on that table, not on the group-by entity."
					),
				}
			if not data.get("is_standard"):
				data["is_standard"] = "No"
				meta["is_standard"] = {
					"source": "default",
					"rationale": (
						"Handoff-generated reports default to site-local "
						"(is_standard=No) so they don't need "
						"developer_mode + Administrator at save time."
					),
				}
			# TD-M7: forward candidate.filters → data.filters_json so
			# the Query Report opens with the date-range filter
			# defaults pre-filled. User can change the window at
			# runtime without editing the SQL.
			if cand_filters and not data.get("filters_json"):
				data["filters_json"] = json.dumps(cand_filters)
				meta["filters_json"] = {
					"source": "default",
					"rationale": (
						"Filters for the %(from_date)s / %(to_date)s "
						"placeholders in the SQL. Defaults came from the "
						"Insights prompt's time range; user can change "
						"at runtime to re-run for a different window."
					),
				}
