"""Structured return type for the Insights mode handler.

``ReportCandidate`` carries the query shape an Insights handler can emit
alongside its natural-language reply. When a prompt is report-shaped
(tabular, filterable, aggregation-ready) the handler attaches a candidate
so the client can offer "Save as Report" as a one-click handoff into
Dev mode with the create_report intent.

Spec: docs/specs/2026-04-22-insights-to-report-handoff.md.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ReportCandidate(BaseModel):
	"""Structured query shape emitted by Insights for optional Report handoff.

	``target_doctype`` is the only required field. Everything else is
	optional so the extractor can populate whatever the user's prompt made
	clear without fabricating the rest.
	"""

	target_doctype: str
	report_type: str = "Report Builder"
	columns: list[dict[str, Any]] = Field(default_factory=list)
	filters: list[dict[str, Any]] = Field(default_factory=list)
	sort: list[dict[str, Any]] = Field(default_factory=list)
	limit: int | None = None
	time_range: dict[str, Any] | None = None
	suggested_name: str | None = None

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
	report_candidate: ReportCandidate | None = None
