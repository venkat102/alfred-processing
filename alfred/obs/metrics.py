"""Prometheus metrics for Alfred's processing pipeline.

All metrics live in the default Prometheus client registry so
`prometheus_client.make_asgi_app()` picks them up on /metrics without
needing to thread a registry through every call site.

Four metrics cover the operational surface:

- `alfred_pipeline_phase_duration_seconds` (histogram): how long each
  of the 12 phases takes end-to-end. Catches "warmup suddenly taking
  30s" or "run_crew timeout regressions" without needing a tracer.

- `alfred_mcp_calls_total` (counter): every MCP tool invocation grouped
  by tool + outcome. Catches tool-call loops and failure spikes in the
  live UI stream that the agent traces won't.

- `alfred_orchestrator_decisions_total` (counter): how the mode was
  picked. Gives proof of whether the classifier LLM is actually running
  in production vs the fallback always firing (the bug that bit us
  before the urllib migration).

- `alfred_llm_errors_total` (counter): one tick per OllamaError, sliced
  by tier + error type. Feeds alerting when Ollama is down.

Zero attempt to track LLM call SUCCESS throughput here - that's what
the tracer's span duration already captures. This module is for things
the tracer doesn't natively expose.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

# Using the default registry so make_asgi_app() finds everything without
# explicit threading. If tests need isolation they can reset via the
# `reset_for_tests()` helper below.
_registry: CollectorRegistry = DEFAULT_REGISTRY

pipeline_phase_duration_seconds = Histogram(
	"alfred_pipeline_phase_duration_seconds",
	"Time spent in each pipeline phase, in seconds.",
	labelnames=("phase",),
	# Buckets chosen for the real distribution: triage phases are sub-
	# second, enhance is ~2-10s, run_crew can be 60-300s.
	buckets=(0.05, 0.25, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 180.0, 600.0),
)

mcp_calls_total = Counter(
	"alfred_mcp_calls_total",
	"Total MCP tool invocations, grouped by tool and outcome.",
	labelnames=("tool", "outcome"),
)

orchestrator_decisions_total = Counter(
	"alfred_orchestrator_decisions_total",
	"How the orchestrator picked the mode. Source is one of "
	"override / fast_path / classifier / fallback.",
	labelnames=("source", "mode"),
)

llm_errors_total = Counter(
	"alfred_llm_errors_total",
	"OllamaError occurrences in standalone LLM calls, by tier and error type.",
	labelnames=("tier", "error_type"),
)

# These two counters exist specifically to quantify CrewAI's
# framework-quirk tax. The Developer agent sometimes pivots out of the
# task structure and emits prose or training-data dumps instead of a
# changeset ("drift"). When that happens we invoke a direct LLM call to
# regenerate the changeset ("rescue"). Both paths cost extra latency +
# tokens, so we want numbers - not vibes - to tell us whether the
# framework is earning its keep.
crew_drift_total = Counter(
	"alfred_crew_drift_total",
	"How often the crew's Developer agent drifts off the task structure. "
	"Reason is the classifier keyword from _detect_drift (e.g. "
	"'training_data_dump', 'prose_only'). High rates mean the framework "
	"quirks are actively costing us.",
	labelnames=("reason",),
)

crew_rescue_total = Counter(
	"alfred_crew_rescue_total",
	"How often the rescue LLM regeneration path ran, and whether it "
	"produced a usable changeset. outcome is 'produced' or 'empty'. "
	"A growing 'empty' share means rescue is not recovering real misses.",
	labelnames=("outcome",),
)

rate_limit_block_total = Counter(
	"alfred_rate_limit_block_total",
	"Requests blocked by the per-user rate limit. source=rest|websocket "
	"distinguishes the entry path; a spike on websocket with rest flat "
	"usually means a compromised client is flooding prompts (LLM DoS / "
	"cost exhaustion).",
	labelnames=("source",),
)

ssrf_block_total = Counter(
	"alfred_ssrf_block_total",
	"Outbound LLM URLs rejected by the SSRF allow-list. reason=bad_scheme "
	"|no_host|dns_fail|private_ip|host_not_allowed. A spike on private_ip "
	"usually means a compromised client is probing for internal services "
	"(cloud metadata, Redis, admin portals).",
	labelnames=("reason",),
)


def reset_for_tests() -> None:
	"""Reset all metric samples. Call from test setup only.

	prometheus_client doesn't ship a public reset; we poke the internal
	`_metrics` dict on each labelled collector. Safe because we're the
	only writers.
	"""
	for m in (
		pipeline_phase_duration_seconds,
		mcp_calls_total,
		orchestrator_decisions_total,
		llm_errors_total,
		crew_drift_total,
		crew_rescue_total,
		rate_limit_block_total,
		ssrf_block_total,
	):
		try:
			m._metrics.clear()  # type: ignore[attr-defined]
		except Exception:  # noqa: BLE001
			pass
