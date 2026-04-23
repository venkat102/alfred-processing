"""Structured return type for the Insights mode handler.

``ReportCandidate`` carries the query shape an Insights handler can emit
alongside its natural-language reply. When a prompt is report-shaped
(tabular, filterable, aggregation-ready) the handler attaches a candidate
so the client can offer "Save as Report" as a one-click handoff into
Dev mode with the create_report intent.

Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ReportCandidate(BaseModel):
	"""Structured query shape emitted by Insights for optional Report handoff.

	``target_doctype`` is the only required field. Everything else is
	optional so the extractor can populate whatever the user's prompt made
	clear without fabricating the rest.

	Aggregation queries (``top N <entity> by <metric>``, ``<metric> by
	<entity>``) use ``query`` + ``aggregation`` to carry a ready-made
	Query Report SQL body. When these are set, the handoff asks the
	specialist to emit a Query Report (report_type="Query Report") with
	the exact SQL in ``data.query``. The pipeline safety net
	(``_phase_post_crew``) fills the fields deterministically if the
	specialist skipped them.
	"""

	target_doctype: str
	report_type: str = "Report Builder"
	columns: list[dict[str, Any]] = Field(default_factory=list)
	filters: list[dict[str, Any]] = Field(default_factory=list)
	sort: list[dict[str, Any]] = Field(default_factory=list)
	limit: Optional[int] = None
	time_range: Optional[dict[str, Any]] = None
	suggested_name: Optional[str] = None
	# Query Report SQL body, pre-rendered when the extractor detected an
	# aggregation pattern. None for simple list-shape (Report Builder) reports.
	query: Optional[str] = None
	# Metadata describing the aggregation:
	#   source_doctype, metric_field, metric_fn, metric_label,
	#   group_by_field, group_by_label
	# Carried alongside ``query`` so the specialist (and UI preview) can
	# describe what was built without re-parsing the SQL.
	aggregation: Optional[dict[str, Any]] = None

	def to_handoff_prompt(self) -> str:
		"""Render the candidate as a human-readable block for a Dev prompt.

		Every non-empty field becomes one line. The whole block is meant
		to be attached to a Dev-mode chat message so the classifier (via
		heuristic intent patterns) + pipeline (via __report_candidate__
		marker) can route it to create_report without re-interpretation.
		"""
		parts = [
			"Save as Report:",
			f"Source DocType: {self.target_doctype}",
			f"Report type: {self.report_type}",
		]
		if self.suggested_name:
			parts.append(f"Suggested name: {self.suggested_name}")
		if self.columns:
			parts.append("Columns: " + ", ".join(
				c.get("label") or c.get("fieldname", "?") for c in self.columns
			))
		if self.filters:
			parts.append("Filters: " + ", ".join(
				f"{f.get('fieldname')} {f.get('operator', '=')} {f.get('value')}"
				for f in self.filters
			))
		if self.sort:
			parts.append("Sort: " + ", ".join(
				f"{s.get('fieldname')} {s.get('direction', 'asc').upper()}"
				for s in self.sort
			))
		if self.limit:
			parts.append(f"Limit: {self.limit}")
		if self.time_range:
			rng = self.time_range
			parts.append(
				f"Time range: {rng.get('field', 'date')} in "
				f"{rng.get('preset', rng.get('value', ''))}"
			)
		if self.aggregation:
			agg = self.aggregation
			parts.append(
				"Aggregation: "
				f"{agg.get('metric_fn', 'SUM')}({agg.get('metric_field', '?')}) "
				f"grouped by {agg.get('group_by_field', '?')} "
				f"on {agg.get('source_doctype', '?')}"
			)
		if self.query:
			# Fence the SQL so prompt-level interpolation (.format) doesn't
			# mangle braces and so the specialist sees it as a literal block
			# to copy into data.query verbatim.
			parts.append("Query (copy verbatim into data.query):")
			parts.append("```sql")
			parts.append(self.query)
			parts.append("```")
		return "\n".join(parts)


class InsightsResult(BaseModel):
	"""Return shape for ``handle_insights``.

	``reply`` is the natural-language answer the pipeline will emit as
	``insights_reply``. ``report_candidate`` is attached when the query is
	report-shaped and the site returned data; None otherwise. Clients read
	``report_candidate`` to decide whether to render a "Save as Report"
	button.
	"""

	reply: str
	report_candidate: Optional[ReportCandidate] = None
