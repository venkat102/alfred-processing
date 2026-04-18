"""FKB retrieval benchmark: keyword vs semantic.

Goal: measure whether dropping sentence-transformers would cost us
accuracy. The semantic layer pulls ~600 MB of torch + transformers +
the MiniLM model weights; if keyword-only is within a few points on
these queries, that's a big install-size win.

How to read the output:
- MRR (Mean Reciprocal Rank): higher is better. 1.0 means the gold
  entry is always ranked #1. 0.5 means on average it's ranked #2.
- "hit@3": percentage of queries where the gold entry lands in top 3.
- "mean rank": average position of the gold entry (lower is better).
  Queries that miss the cutoff entirely count as rank=infinity (we
  use the k+1 sentinel so they're penalised but don't nuke the mean).

The query set is hand-curated to mirror the kind of prompts that reach
the pipeline's enhance phase - short and technical, often phrased with
natural-language intent rather than exact keywords.

Run:
    .venv/bin/python tools/fkb_benchmark.py
"""

from __future__ import annotations

import os
import sys
import time

# Allow running from repo root. The package is installed editable so this
# is belt-and-suspenders for `python tools/foo.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alfred.knowledge import fkb  # noqa: E402


# Hand-curated query -> expected "gold" entry id. The gold is what a human
# operator would pick as the single most-relevant entry; the scoring also
# credits near-neighbours but MRR reports on the gold.
QUERIES: list[tuple[str, str]] = [
	# Rules
	("can I use import in a server script", "server_script_no_imports"),
	("how do I check permission before writing in a server script", "server_script_permission_check"),
	("client script cannot call frappe.db", "client_script_sandbox_model"),
	("should I use notification or server script for sending emails", "notification_doctype_vs_server_script"),
	("when to add a custom field vs a new DocType", "custom_field_vs_new_doctype"),
	("what fields does a Workflow need", "workflow_requirements"),
	("DocType name rules", "doctype_naming_constraints"),
	("don't over-reach, only build what the user asked for", "minimal_change_principle"),
	# Idioms
	("hook into before_save on Sales Order from an app", "idiom_hooks_doc_events"),
	("lifecycle hooks for submit and cancel", "idiom_submit_lifecycle"),
	("how to rename a DocType and keep links intact", "idiom_rename_flow"),
	("schedule a daily cleanup job", "idiom_scheduler_events"),
	("send a background job from a server action", "idiom_background_job_enqueue"),
	("auto assign a document to a user based on rules", "idiom_assignment_rule"),
	("Jinja template for a PDF invoice", "idiom_print_format_jinja"),
	("row-level filter so users only see their own records", "idiom_permission_query_conditions"),
	("naming series for invoices", "idiom_naming_series"),
	("publish realtime events to the browser", "idiom_redis_pubsub_realtime"),
	# Style
	("how should I raise errors that reach the user", "style_user_facing_errors_use_throw"),
	("translate user-visible strings", "style_translate_user_strings"),
	("protect against SQL injection when building queries", "style_sql_injection_safe"),
	("use snake_case for field names", "style_snake_case_fieldnames"),
	# API
	("how do I fetch a single field from the database fast", "api_frappe_db_get_value"),
	("throw an exception that shows to the user", "api_frappe_throw"),
	("send an email with frappe", "api_frappe_sendmail"),
]


def _rank_of(hits: list[dict], gold_id: str) -> int | None:
	"""Return 1-based rank of gold_id in hits, or None if absent."""
	for i, hit in enumerate(hits, start=1):
		if hit.get("id") == gold_id:
			return i
	return None


def _mrr(ranks: list[int | None], miss_penalty_rank: int) -> float:
	"""Mean Reciprocal Rank. Misses contribute 1/miss_penalty_rank."""
	total = 0.0
	for r in ranks:
		if r is None:
			total += 1.0 / miss_penalty_rank
		else:
			total += 1.0 / r
	return total / len(ranks) if ranks else 0.0


def main():
	k = 5  # top-k to consider
	miss_penalty = k + 1  # misses get a rank just outside the cutoff

	# Warm up the semantic layer: first call pays the model-load cost.
	print("Warming up semantic model (first call pays model-load cost)...")
	_t = time.perf_counter()
	_ = fkb.search_semantic("warmup", k=1)
	model_load_s = time.perf_counter() - _t
	print(f"  Semantic model ready in {model_load_s:.2f}s\n")

	kw_ranks: list[int | None] = []
	sem_ranks: list[int | None] = []
	hyb_ranks: list[int | None] = []
	kw_time = 0.0
	sem_time = 0.0
	hyb_time = 0.0

	print(f"{'Query (first 50)':<55}  {'Gold':<40}  Kw  Sem  Hyb")
	print("-" * 120)

	for query, gold in QUERIES:
		t0 = time.perf_counter()
		kw = fkb.search_keyword(query, k=k)
		kw_time += time.perf_counter() - t0

		t0 = time.perf_counter()
		sem = fkb.search_semantic(query, k=k)
		sem_time += time.perf_counter() - t0

		t0 = time.perf_counter()
		hyb = fkb.search_hybrid(query, k=k)
		hyb_time += time.perf_counter() - t0

		kw_r = _rank_of(kw, gold)
		sem_r = _rank_of(sem, gold)
		hyb_r = _rank_of(hyb, gold)
		kw_ranks.append(kw_r)
		sem_ranks.append(sem_r)
		hyb_ranks.append(hyb_r)

		print(
			f"{query[:54]:<55}  {gold[:39]:<40}  "
			f"{str(kw_r or '-'):<3} {str(sem_r or '-'):<4} {str(hyb_r or '-')}"
		)

	def _hit_at(ranks: list[int | None], n: int) -> float:
		return sum(1 for r in ranks if r is not None and r <= n) / len(ranks)

	def _mean_rank(ranks: list[int | None]) -> float:
		return sum((r if r is not None else miss_penalty) for r in ranks) / len(ranks)

	n = len(QUERIES)
	print()
	print("=" * 120)
	print(f"Query set: {n} queries, top-k={k}, miss-penalty-rank={miss_penalty}")
	print()
	print(f"{'Metric':<16}  {'Keyword':>12}  {'Semantic':>12}  {'Hybrid':>12}")
	print("-" * 60)
	print(f"{'MRR':<16}  {_mrr(kw_ranks, miss_penalty):>12.4f}  "
	      f"{_mrr(sem_ranks, miss_penalty):>12.4f}  {_mrr(hyb_ranks, miss_penalty):>12.4f}")
	print(f"{'hit@1':<16}  {_hit_at(kw_ranks, 1):>12.2%}  "
	      f"{_hit_at(sem_ranks, 1):>12.2%}  {_hit_at(hyb_ranks, 1):>12.2%}")
	print(f"{'hit@3':<16}  {_hit_at(kw_ranks, 3):>12.2%}  "
	      f"{_hit_at(sem_ranks, 3):>12.2%}  {_hit_at(hyb_ranks, 3):>12.2%}")
	print(f"{'hit@5':<16}  {_hit_at(kw_ranks, 5):>12.2%}  "
	      f"{_hit_at(sem_ranks, 5):>12.2%}  {_hit_at(hyb_ranks, 5):>12.2%}")
	print(f"{'mean rank':<16}  {_mean_rank(kw_ranks):>12.2f}  "
	      f"{_mean_rank(sem_ranks):>12.2f}  {_mean_rank(hyb_ranks):>12.2f}")
	print(f"{'total time (ms)':<16}  {kw_time*1000:>12.1f}  "
	      f"{sem_time*1000:>12.1f}  {hyb_time*1000:>12.1f}")
	print(f"{'per-query (ms)':<16}  {kw_time*1000/n:>12.1f}  "
	      f"{sem_time*1000/n:>12.1f}  {hyb_time*1000/n:>12.1f}")
	print()
	print(f"Model load cost (first call): {model_load_s:.2f}s")
	print()
	print("Interpretation guide:")
	print("  - If keyword MRR is within ~5% of hybrid and hit@3 is comparable,")
	print("    sentence-transformers is not pulling its weight - drop it and")
	print("    save ~600 MB + the ~4s cold-start model load.")
	print("  - If semantic wins decisively on MRR or hit@3, keep hybrid and")
	print("    cache embeddings to disk to cut cold-start.")


if __name__ == "__main__":
	main()
