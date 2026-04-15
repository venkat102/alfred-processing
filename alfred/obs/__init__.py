"""Observability primitives for the Alfred pipeline.

`tracer` is a tiny span/context-manager API that records per-phase timings,
token counts, tool calls, and errors, then hands each finished span to
registered exporters (JSONL file by default, stdout optional).

Deliberately does NOT depend on opentelemetry-api. We can swap in the
OTel SDK later if we need to export to Jaeger/Honeycomb/Langfuse without
touching call sites - they use the same `async with tracer.span(...)`
shape.
"""

from alfred.obs.tracer import (
	Span,
	Tracer,
	configure_from_env,
	jsonl_file_exporter,
	stdout_exporter,
	tracer,
)

__all__ = [
	"Span",
	"Tracer",
	"tracer",
	"jsonl_file_exporter",
	"stdout_exporter",
	"configure_from_env",
]
