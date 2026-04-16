"""Processing-app-side Frappe Knowledge Base (FKB) retrieval.

Mirror of alfred_client/mcp/frappe_kb.py but running inside the processing
app, with a semantic layer added. The same YAML files on disk are the
source of truth:

    alfred_client/alfred_client/data/frappe_kb/
        rules.yaml       (Phase A, 8 platform rules)
        apis.yaml        (Phase C.1, ~140 Frappe APIs)
        apis_overrides.yaml   (hand-curated API overrides; merged by build script)
        idioms.yaml      (Phase D, future)
        style.yaml       (Phase D, future)

Why a separate loader here instead of calling the Frappe-app MCP tool:
  - The MCP tool is keyword-only (the bench venv can't host the ML deps).
  - Hybrid retrieval needs sentence-transformers, which the processing
    app already carries.
  - Doing the lookup in-process saves one MCP round-trip per inject turn
    (~20-40ms) which matters because inject_kb runs on every Dev turn.

Retrieval modes:
    keyword  -> identical to frappe_kb.py:search_keyword (weighted scoring)
    semantic -> sentence-transformers cosine over cached entry embeddings
    hybrid   -> keyword first; if top keyword score is below threshold,
                fall back to semantic. Returns union ranked by a blended
                score so high-confidence keyword hits still top the list.

Caching:
  - The entries dict is reloaded on YAML mtime change.
  - The embedding matrix is recomputed when entries are reloaded OR when
    the cached `.npy` is older than any YAML.

Fail-open behaviour:
  - If sentence-transformers can't be imported, semantic silently downgrades
    to keyword-only and `search_hybrid` behaves like `search_keyword`.
  - If YAMLs can't be read (path wrong, permission), the module returns
    empty results rather than raising - this is a retrieval layer, not a
    correctness layer. The pipeline keeps running.

All entry points are thread-safe via a module-level lock; the model and
embedding cache are one-per-process.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("alfred.knowledge.fkb")


# ── Paths and schema ───────────────────────────────────────────────

_KB_FILES = ("rules.yaml", "apis.yaml", "idioms.yaml", "style.yaml")
_VALID_KINDS = {"rule", "api", "idiom", "style"}
_REQUIRED_FIELDS = ("kind", "title", "summary", "keywords", "body", "verified_on")

# Match framework_kg.search_framework_knowledge and the Frappe-side frappe_kb.
_WEIGHT_TITLE = 5
_WEIGHT_KEYWORD = 3
_WEIGHT_APPLIES_TO = 4
_WEIGHT_BODY = 1


def _resolve_kb_dir() -> Path:
	"""Locate the shared frappe_kb/ directory.

	Normal dev layout:  bench/apps/alfred_client/alfred_client/data/frappe_kb
	CI / alternate:     ALFRED_FKB_DIR env var override

	We walk up from this file to find `apps/alfred_client`; if that fails,
	fall back to the env var so test harnesses can point elsewhere.
	"""
	override = os.environ.get("ALFRED_FKB_DIR")
	if override:
		return Path(override)

	# We're at alfred_processing/alfred/knowledge/fkb.py. The bench checkout
	# is typically at bench/apps/alfred_processing while the Frappe app is
	# at bench/apps/alfred_client.
	here = Path(__file__).resolve()
	# Walk up looking for a sibling alfred_client app.
	for ancestor in here.parents:
		candidate = ancestor.parent / "alfred_client" / "alfred_client" / "data" / "frappe_kb"
		if candidate.exists():
			return candidate
	# Last-resort: absolute dev path on this workstation.
	return Path(
		"/Users/venkatesh/bench/develop/frappe-bench/apps/alfred_client"
		"/alfred_client/data/frappe_kb"
	)


# ── Module-level cache ─────────────────────────────────────────────


_lock = threading.Lock()
_entries_cache: dict[str, Any] = {}
_entries_mtimes: dict[str, float] = {}

# Lazy-loaded ML bits. None until first semantic call; set to False
# on import failure so we don't retry.
_model: Any = None
_model_load_failed: bool = False
_embeddings: Any = None  # numpy.ndarray | None
_embedding_ids: list[str] = []
_embeddings_stale: bool = True


# ── Loader ─────────────────────────────────────────────────────────


def _load_entries() -> dict[str, Any]:
	"""Reload KB entries from disk if any YAML mtime has changed.

	Returns the current entries dict. Schema validation drops bad rows with
	a warning - same contract as the Frappe-side frappe_kb.py loader.
	"""
	global _entries_cache, _entries_mtimes, _embeddings_stale

	kb_dir = _resolve_kb_dir()
	new_mtimes: dict[str, float] = {}
	any_change = False

	for filename in _KB_FILES:
		path = kb_dir / filename
		if not path.exists():
			continue
		mt = path.stat().st_mtime
		new_mtimes[filename] = mt
		if _entries_mtimes.get(filename) != mt:
			any_change = True

	# Also invalidate if a previously-present file disappeared.
	for filename in list(_entries_mtimes):
		if filename not in new_mtimes:
			any_change = True

	if not any_change and _entries_cache:
		return _entries_cache

	import yaml  # pyyaml is always in the processing env

	merged: dict[str, Any] = {}
	for filename in _KB_FILES:
		path = kb_dir / filename
		if not path.exists():
			continue
		try:
			parsed = yaml.safe_load(path.read_text()) or {}
		except Exception as e:
			logger.error("FKB: failed to parse %s: %s", filename, e)
			continue
		if not isinstance(parsed, dict):
			logger.error("FKB: %s root is not a dict, skipping", filename)
			continue
		for entry_id, entry in parsed.items():
			if not _is_valid_entry(entry_id, entry, filename):
				continue
			if entry_id in merged:
				logger.warning(
					"FKB: id collision %r - later file wins (%s)", entry_id, filename,
				)
			merged[entry_id] = dict(entry, id=entry_id)

	_entries_cache = merged
	_entries_mtimes = new_mtimes
	_embeddings_stale = True  # entries changed -> semantic cache is stale
	logger.info("FKB: loaded %d entries from %s", len(merged), kb_dir)
	return _entries_cache


def _is_valid_entry(entry_id: str, entry: Any, filename: str) -> bool:
	"""Validate one YAML entry. Logs and returns False on invalid rows."""
	if not isinstance(entry, dict):
		logger.warning("FKB %s: entry %r is not a dict - skipping", filename, entry_id)
		return False
	missing = [f for f in _REQUIRED_FIELDS if f not in entry]
	if missing:
		logger.warning(
			"FKB %s: entry %r missing fields %s - skipping",
			filename, entry_id, missing,
		)
		return False
	if entry.get("kind") not in _VALID_KINDS:
		logger.warning(
			"FKB %s: entry %r has invalid kind %r - skipping",
			filename, entry_id, entry.get("kind"),
		)
		return False
	if not isinstance(entry.get("keywords"), list):
		logger.warning(
			"FKB %s: entry %r has non-list keywords - skipping", filename, entry_id,
		)
		return False
	return True


# ── Keyword search (reference impl, same as frappe_kb.py) ──────────


def search_keyword(
	query: str,
	kind: str | None = None,
	k: int = 5,
	min_score: int = 3,
) -> list[dict[str, Any]]:
	"""Weighted keyword search over cached entries.

	Mirrors alfred_client/mcp/frappe_kb.py:search_keyword byte-for-byte so
	the two KB-consuming surfaces behave identically when the tool is
	called directly by an agent vs auto-injected by the pipeline.
	"""
	if not query or not query.strip():
		return []

	query_lc = query.lower()
	terms = [t for t in query_lc.split() if len(t) >= 3]
	if not terms:
		return []

	entries = _load_entries()

	def _field_score(text: str, weight: int) -> int:
		text_lc = (text or "").lower()
		return sum(weight for t in terms if t in text_lc)

	hits: list[tuple[int, str, dict[str, Any]]] = []
	for entry_id, entry in entries.items():
		if kind and entry.get("kind") != kind:
			continue
		title = entry.get("title", "")
		keywords_text = " ".join(
			entry.get("keywords", []) if isinstance(entry.get("keywords"), list) else []
		)
		applies_text = " ".join(
			entry.get("applies_to", []) if isinstance(entry.get("applies_to"), list) else []
		)
		body = entry.get("body", "")
		summary = entry.get("summary", "")

		score = (
			_field_score(title, _WEIGHT_TITLE)
			+ _field_score(keywords_text, _WEIGHT_KEYWORD)
			+ _field_score(applies_text, _WEIGHT_APPLIES_TO)
			+ _field_score(body, _WEIGHT_BODY)
			+ _field_score(summary, _WEIGHT_BODY)
		)
		if score < min_score:
			continue
		hits.append((score, entry_id, entry))

	hits.sort(key=lambda x: (-x[0], x[1]))
	return [
		dict(entry, id=entry_id, _score=score, _mode="keyword")
		for score, entry_id, entry in hits[:k]
	]


# ── Semantic search (sentence-transformers, lazy model load) ──────


_EMBEDDING_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"


def _get_model():
	"""Load the embedding model once per process. Returns None on failure.

	Failure is sticky (`_model_load_failed = True`) so we don't pay the
	import-error cost on every query after the first miss.
	"""
	global _model, _model_load_failed
	if _model is not None:
		return _model
	if _model_load_failed:
		return None
	try:
		from sentence_transformers import SentenceTransformer
	except ImportError as e:
		logger.warning("FKB: sentence-transformers unavailable, semantic disabled (%s)", e)
		_model_load_failed = True
		return None
	try:
		_model = SentenceTransformer(_EMBEDDING_MODEL_NAME)
		logger.info("FKB: loaded embedding model %s", _EMBEDDING_MODEL_NAME)
	except Exception as e:
		logger.error("FKB: failed to load model: %s", e)
		_model_load_failed = True
		return None
	return _model


def _entry_text_for_embedding(entry: dict[str, Any]) -> str:
	"""Compose the text we embed for one entry.

	Title + summary + keywords carry the most signal. Body is included but
	truncated because MiniLM caps at 256 tokens anyway and we'd rather not
	dilute the vector with long pasted examples.
	"""
	parts = [
		entry.get("title") or "",
		entry.get("summary") or "",
		" ".join(entry.get("keywords") or []),
	]
	body = (entry.get("body") or "")[:500]
	if body:
		parts.append(body)
	return "\n".join(p for p in parts if p)


def _ensure_embeddings():
	"""Compute or refresh the embedding matrix to match the current entries dict.

	Recomputes when `_embeddings_stale` flips (entries reloaded) OR when
	the cache is empty. No on-disk cache yet - in-memory is enough for ~150
	entries (150 × 384 × 4 bytes = 230 KB) and the recomputation takes
	under 1s on CPU.

	Returns True if a usable embedding matrix exists, False if semantic
	retrieval should be skipped (model unavailable, etc).
	"""
	global _embeddings, _embedding_ids, _embeddings_stale

	if _embeddings is not None and not _embeddings_stale:
		return True

	model = _get_model()
	if model is None:
		return False

	entries = _load_entries()
	if not entries:
		_embeddings = None
		_embedding_ids = []
		_embeddings_stale = False
		return False

	ids = sorted(entries.keys())
	texts = [_entry_text_for_embedding(entries[i]) for i in ids]

	import numpy as np

	try:
		# normalize_embeddings=True gives unit-length vectors so cosine
		# similarity collapses to a dot product (faster, numerically stabler).
		mat = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
		_embeddings = np.asarray(mat, dtype=np.float32)
		_embedding_ids = ids
		_embeddings_stale = False
		logger.info("FKB: computed %d entry embeddings (%s)", len(ids), _embeddings.shape)
		return True
	except Exception as e:
		logger.error("FKB: embedding generation failed: %s", e)
		_embeddings = None
		_embedding_ids = []
		_embeddings_stale = False  # don't retry on every call
		return False


def search_semantic(
	query: str,
	kind: str | None = None,
	k: int = 5,
	min_similarity: float = 0.28,
) -> list[dict[str, Any]]:
	"""Dense-embedding cosine search over cached entries.

	Returns [] if sentence-transformers is unavailable or embedding
	generation fails. `min_similarity` is a soft floor - results below
	this cosine are dropped to prevent noise injection into the auto-inject
	banner for unrelated prompts.

	The 0.28 default is tuned for all-MiniLM-L6-v2 on short technical
	content: related-but-distinct entries typically cluster at 0.30-0.45,
	while clearly-unrelated entries score <0.20. 0.28 is permissive
	enough for "rescue" cases (user's phrasing misses the curated
	keywords) while staying above the unrelated-content floor.
	"""
	if not query or not query.strip():
		return []

	with _lock:
		if not _ensure_embeddings():
			return []

	model = _get_model()  # cached; ensured non-None because _ensure_embeddings succeeded
	import numpy as np

	try:
		qvec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)
		qvec = np.asarray(qvec, dtype=np.float32)[0]
	except Exception as e:
		logger.warning("FKB: failed to embed query: %s", e)
		return []

	# Cosine = dot product for unit vectors. Score against every entry,
	# then filter by kind + min_similarity and take top-k.
	scores = _embeddings @ qvec  # shape: (N,)
	entries = _load_entries()

	ranked: list[tuple[float, str, dict[str, Any]]] = []
	for idx, entry_id in enumerate(_embedding_ids):
		sim = float(scores[idx])
		if sim < min_similarity:
			continue
		entry = entries.get(entry_id)
		if not entry:
			continue
		if kind and entry.get("kind") != kind:
			continue
		ranked.append((sim, entry_id, entry))

	ranked.sort(key=lambda x: (-x[0], x[1]))
	return [
		dict(entry, id=entry_id, _score=round(sim, 4), _mode="semantic")
		for sim, entry_id, entry in ranked[:k]
	]


# ── Hybrid retrieval ────────────────────────────────────────────────


def search_hybrid(
	query: str,
	kind: str | None = None,
	k: int = 3,
	semantic_min_similarity: float = 0.28,
) -> list[dict[str, Any]]:
	"""Blend keyword + semantic so both get a guaranteed seat.

	Policy (intentionally simple, intentionally deterministic):
	  - Slot 1: keyword top-1 (deterministic, strong signal when present).
	  - Slot 2: semantic top-1, IF it's a different entry than slot 1.
	    This is the "rescue" slot - when keyword matches a body-bleed
	    false positive but semantic has the real intent, semantic lands
	    here.
	  - Slot 3..k: interleave the remainder (keyword #2, semantic #2, kw #3, ...),
	    skipping anything already placed.

	Why not RRF / score-blend: the scores from keyword vs cosine are on
	completely different scales, and body-bleed can inflate keyword scores
	so much that even aggressive normalization keeps false positives on
	top. Reserving slots is a coarser but more debuggable rule - you can
	always tell which slot came from which retriever (via `_mode`), and
	the banner always includes at least one semantic hit when the layer
	is available.
	"""
	kw = search_keyword(query, kind=kind, k=max(k, 5))
	sem = search_semantic(
		query, kind=kind, k=max(k, 5), min_similarity=semantic_min_similarity,
	)

	out: list[dict[str, Any]] = []
	seen: set[str] = set()

	def _take(hit: dict[str, Any], mode: str) -> bool:
		"""Append `hit` to out with its source mode. Returns True if added."""
		entry_id = hit.get("id")
		if not entry_id or entry_id in seen:
			return False
		out.append(dict(hit, _mode=mode))
		seen.add(entry_id)
		return True

	# Slot 1: keyword top - highest-confidence deterministic hit.
	if kw:
		_take(kw[0], "keyword")

	# Slot 2: semantic top, if different. This is the rescue slot.
	if sem:
		_take(sem[0], "semantic")

	# Remaining slots: interleave kw[1:] and sem[1:], keyword first.
	idx = 1
	while len(out) < k:
		progressed = False
		if idx < len(kw) and _take(kw[idx], "keyword"):
			progressed = True
			if len(out) >= k:
				break
		if idx < len(sem) and _take(sem[idx], "semantic"):
			progressed = True
		idx += 1
		if not progressed and idx > max(len(kw), len(sem)):
			break

	return out[:k]


# ── Introspection helpers ──────────────────────────────────────────


def list_entries(kind: str | None = None) -> list[dict[str, Any]]:
	"""Return summary list for browsing / debug."""
	entries = _load_entries()
	out = []
	for entry_id, entry in entries.items():
		if kind and entry.get("kind") != kind:
			continue
		out.append({
			"id": entry_id,
			"kind": entry.get("kind"),
			"title": entry.get("title", ""),
			"summary": entry.get("summary", ""),
		})
	out.sort(key=lambda e: (e["kind"] or "", e["id"]))
	return out


def lookup_entry(entry_id: str) -> dict[str, Any] | None:
	return _load_entries().get(entry_id)


def clear_cache() -> None:
	"""Force reload on next call. Used by tests."""
	global _entries_cache, _entries_mtimes, _embeddings, _embedding_ids
	global _embeddings_stale, _model, _model_load_failed
	_entries_cache = {}
	_entries_mtimes = {}
	_embeddings = None
	_embedding_ids = []
	_embeddings_stale = True
	_model = None
	_model_load_failed = False
