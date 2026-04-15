#!/usr/bin/env python3
"""Diff two benchmark reports side-by-side.

Usage:
    .venv/bin/python scripts/compare_benchmarks.py benchmarks/baseline_clean_2026-04-13.json benchmarks/phase1_2026-04-13.json

Compares per-prompt and summary metrics. Flags regressions in red, wins in green.
Exits 1 if the AFTER run regressed on any of: tokens, latency, first-try accuracy.
"""

import argparse
import json
import sys
from pathlib import Path


def _load(path: str) -> dict:
	return json.loads(Path(path).read_text())


def _diff_pct(before: float, after: float) -> str:
	if before == 0:
		return "n/a"
	delta = after - before
	pct = (delta / before) * 100
	sign = "+" if delta >= 0 else ""
	return f"{sign}{pct:.1f}%"


def _fmt_change(before: float, after: float, lower_is_better: bool = True) -> str:
	pct = _diff_pct(before, after)
	if pct == "n/a":
		return f"{before:.1f} -> {after:.1f}"
	delta = after - before
	is_better = (delta < 0) == lower_is_better
	color = "\033[32m" if is_better else "\033[31m"
	return f"{color}{before:.1f} -> {after:.1f} ({pct})\033[0m"


def main():
	parser = argparse.ArgumentParser(description="Compare two benchmark reports")
	parser.add_argument("before", help="Path to baseline JSON report")
	parser.add_argument("after", help="Path to phase1 (or later) JSON report")
	parser.add_argument("--gate", action="store_true",
		help="Exit 1 if the after run regressed on tokens/latency/accuracy (for CI use)")
	args = parser.parse_args()

	before = _load(args.before)
	after = _load(args.after)

	# Index both by prompt_id for side-by-side comparison
	before_by_id = {r["prompt_id"]: r for r in before["results"]}
	after_by_id = {r["prompt_id"]: r for r in after["results"]}

	print(f"\nBEFORE: {args.before}")
	print(f"  model: {before.get('model')}")
	print(f"  timestamp: {before.get('timestamp')}")
	print(f"\nAFTER:  {args.after}")
	print(f"  model: {after.get('model')}")
	print(f"  timestamp: {after.get('timestamp')}")

	print(f"\n{'=' * 90}")
	print(f"{'Prompt':<35} {'Metric':<15} {'Before':<12} {'After':<12} {'Delta':<12}")
	print(f"{'=' * 90}")

	for pid in sorted(before_by_id.keys()):
		b = before_by_id.get(pid)
		a = after_by_id.get(pid)
		if not b or not a:
			continue
		name = b.get("prompt_name", f"prompt_{pid}")[:34]
		# Skip prompts that didn't execute in one of the runs
		if (b.get("llm_completion_count") or 0) == 0 or (a.get("llm_completion_count") or 0) == 0:
			print(f"{name:<35} [SKIPPED - not executed in one of the runs]")
			continue

		print(f"{name:<35} {'tokens':<15} {_fmt_change(b['llm_total_tokens'], a['llm_total_tokens'])}")
		print(f"{'':35} {'latency_s':<15} {_fmt_change(b['wall_clock_seconds'], a['wall_clock_seconds'])}")
		print(f"{'':35} {'llm_calls':<15} {_fmt_change(b['llm_completion_count'], a['llm_completion_count'])}")
		print(f"{'':35} {'mcp_calls':<15} {_fmt_change(b['mcp_tool_calls'], a['mcp_tool_calls'])}")
		dedup_a = a.get("dedup_hits", 0)
		if dedup_a:
			print(f"{'':35} {'dedup_hits':<15} {'-':<12} {dedup_a:<12} (Phase 1 only)")
		print()

	print(f"{'=' * 90}")
	print(f"SUMMARY")
	print(f"{'=' * 90}")
	bs = before["summary"]
	as_ = after["summary"]

	def _cmp(key, label, lower_is_better=True):
		b = bs.get(key, 0) or 0
		a = as_.get(key, 0) or 0
		print(f"  {label:<28} {_fmt_change(b, a, lower_is_better)}")

	_cmp("avg_wall_clock_seconds", "Avg wall-clock (s)", True)
	_cmp("avg_llm_total_tokens", "Avg LLM tokens", True)
	_cmp("avg_llm_completion_count", "Avg LLM calls", True)
	_cmp("avg_mcp_tool_calls", "Avg MCP calls", True)
	_cmp("first_try_success_rate", "First-try accuracy", False)

	# Gate check
	regressions = []
	if as_.get("avg_llm_total_tokens", 0) > bs.get("avg_llm_total_tokens", 0) * 1.02:
		regressions.append("tokens regressed > 2%")
	if as_.get("avg_wall_clock_seconds", 0) > bs.get("avg_wall_clock_seconds", 0) * 1.10:
		regressions.append("latency regressed > 10%")
	if as_.get("first_try_success_rate", 0) < bs.get("first_try_success_rate", 0):
		regressions.append("first-try accuracy dropped")

	if regressions:
		print(f"\n\033[31mREGRESSIONS DETECTED:\033[0m")
		for r in regressions:
			print(f"  - {r}")
		if args.gate:
			print(f"\nGate failed.")
			sys.exit(1)
	else:
		print(f"\n\033[32mNo regressions.\033[0m Phase 1 is cleared.")

	print()


if __name__ == "__main__":
	main()
