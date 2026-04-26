"""Per-conversation memory for multi-turn follow-up prompts.

A user who says "now add a description field to that DocType" in their second
prompt has no way to succeed if every pipeline run starts from a blank slate.
This module keeps a tiny, structured record of what happened earlier in the
same chat - items discussed, user clarifications, recent raw prompts - and
renders it into the prompt enhancer's input so the LLM can resolve "that"
deterministically.

Scope and deliberate limits:
  - One ConversationMemory per conversation_id. New conversation = fresh state.
  - Stored under `conv-memory-{conversation_id}` via the existing StateStore
    task-state CRUD, so there's no new Redis key shape to manage.
  - No TTL for v1 - conversations persist until the user deletes them.
  - Bounded: clarifications and prompts keep only the N most recent entries so
    a long chat can't grow the memory unbounded.
  - "Items" are structured (doctype + name) not prose, so the render step
    produces a stable, greppable context block rather than drifting summary.

Intentionally NOT a long-term RAG store: no embeddings, no cross-conversation
recall, no user-profile layer. That belongs in a separate system when we get
there. This one just plugs the "followup prompts don't know what was built
earlier" gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("alfred.conversation_memory")

# Caps - trim older entries first when exceeded.
_MAX_ITEMS = 20
_MAX_CLARIFICATIONS = 10
_MAX_PROMPTS = 5
_MAX_INSIGHTS_QUERIES = 10  # Three-mode chat (Phase B)
_INSIGHTS_QUERY_SNIPPET_LEN = 300  # cap per stored answer snippet
_MAX_PLAN_DOCUMENTS = 5  # Three-mode chat (Phase C)
_MAX_PLAN_STEPS_RENDERED = 8  # how many steps to dump in render_for_prompt


def _memory_key(conversation_id: str) -> str:
	return f"conv-memory-{conversation_id}"


@dataclass
class ConversationMemory:
	"""Structured rolling record of what happened in a single conversation."""

	conversation_id: str
	items: list[dict] = field(default_factory=list)
	clarifications: list[dict] = field(default_factory=list)
	recent_prompts: list[str] = field(default_factory=list)
	# Three-mode chat (Phase B+): Q/A pairs from Insights-mode turns so
	# Plan and Dev mode can reference them.
	insights_queries: list[dict] = field(default_factory=list)
	# Three-mode chat (Phase C): list of plan docs proposed in this
	# conversation, capped at _MAX_PLAN_DOCUMENTS. Each entry is a
	# PlanDoc-shaped dict (see alfred.models.plan_doc).
	plan_documents: list[dict] = field(default_factory=list)
	# Three-mode chat (Phase C): the most recent proposed or approved plan
	# doc. Used by the orchestrator's low-confidence fallback - if a plan
	# is active and the user says "build it", fall back to Dev mode. Also
	# injected into _phase_enhance so the Dev crew sees the plan as a
	# CONTEXT block when the user approves it.
	active_plan: dict | None = None
	updated_at: float = field(default_factory=time.time)

	def add_prompt(self, raw_prompt: str) -> None:
		if not raw_prompt:
			return
		self.recent_prompts.append(raw_prompt.strip())
		self.recent_prompts = self.recent_prompts[-_MAX_PROMPTS:]
		self.updated_at = time.time()

	def add_clarifications(self, qa_pairs: list[tuple[str, str]]) -> None:
		"""Append Q/A pairs from the clarifier. Later callers overwrite if
		the same question comes back."""
		if not qa_pairs:
			return
		for q, a in qa_pairs:
			if not q or not a:
				continue
			self.clarifications.append({"q": q.strip(), "a": a.strip()})
		self.clarifications = self.clarifications[-_MAX_CLARIFICATIONS:]
		self.updated_at = time.time()

	def add_insights_query(self, question: str, answer: str) -> None:
		"""Record an Insights-mode Q/A pair for future turns to reference.

		The answer is truncated to avoid unbounded growth when the LLM
		produces a long markdown summary. Question is kept verbatim since
		it's typically short and useful for pronoun resolution in later
		turns ("that workflow I asked about" -> lookup by question).
		"""
		if not question or not answer:
			return
		snippet = answer.strip()
		if len(snippet) > _INSIGHTS_QUERY_SNIPPET_LEN:
			snippet = snippet[:_INSIGHTS_QUERY_SNIPPET_LEN].rstrip() + "..."
		self.insights_queries.append({
			"q": question.strip(),
			"a": snippet,
		})
		self.insights_queries = self.insights_queries[-_MAX_INSIGHTS_QUERIES:]
		self.updated_at = time.time()

	def add_plan_document(
		self, plan: dict, status: str = "proposed"
	) -> None:
		"""Record a plan doc produced by Plan mode.

		Appends to `plan_documents` (bounded), updates `active_plan` to
		point at this one, and stamps the plan with a status so the
		orchestrator's Plan -> Dev handoff logic knows whether the user
		has approved it yet.

		`status` values:
		  - "proposed": just produced, not yet approved by the user
		  - "approved": user clicked Approve & Build, next Dev turn
		    should use it as the spec
		  - "rejected": user discarded it
		  - "built": a Dev run has consumed this plan
		"""
		if not isinstance(plan, dict) or not plan:
			return
		stamped = dict(plan)
		stamped["status"] = status
		self.plan_documents.append(stamped)
		self.plan_documents = self.plan_documents[-_MAX_PLAN_DOCUMENTS:]
		self.active_plan = stamped
		self.updated_at = time.time()

	def mark_active_plan_status(self, status: str) -> None:
		"""Update the status of the active plan in place.

		Used when the user approves or rejects the plan via UI action,
		or when a Dev run consumes an approved plan and should mark it
		"built" so follow-up turns don't re-inject it.
		"""
		if self.active_plan is None:
			return
		self.active_plan = dict(self.active_plan)
		self.active_plan["status"] = status
		# Keep the stored copy in sync so serialisation is consistent.
		if self.plan_documents:
			self.plan_documents[-1] = self.active_plan
		self.updated_at = time.time()

	def add_changeset_items(self, changeset: list[dict]) -> None:
		"""Extract (doctype, name) pairs from a changeset and append to items.

		Silent on malformed items - the memory should never raise on bad
		input, it's a convenience layer not a validator.
		"""
		if not changeset:
			return
		for entry in changeset:
			if not isinstance(entry, dict):
				continue
			doctype = entry.get("doctype") or (entry.get("data") or {}).get("doctype")
			data = entry.get("data") or {}
			name = data.get("name") or data.get("fieldname") or data.get("label")
			op = entry.get("op") or "create"
			if not doctype:
				continue
			record = {
				"op": str(op),
				"doctype": str(doctype),
				"name": str(name) if name else "",
			}
			# Reference DocType for Custom Field / Server Script makes "that
			# field on X" resolvable in followups.
			for k in ("dt", "reference_doctype", "document_type"):
				if data.get(k):
					record["on"] = str(data[k])
					break
			self.items.append(record)
		self.items = self.items[-_MAX_ITEMS:]
		self.updated_at = time.time()

	def render_for_prompt(self) -> str:
		"""Produce the context block to inject into the prompt enhancer.

		Returns an empty string if the memory is empty - callers can then
		skip concatenation and save tokens.
		"""
		if (
			not self.items
			and not self.clarifications
			and not self.recent_prompts
			and not self.insights_queries
			and not self.active_plan
		):
			return ""

		lines: list[str] = ["=== CONVERSATION CONTEXT (earlier in this chat) ==="]

		if self.items:
			lines.append("Already discussed / built:")
			for it in self.items[-_MAX_ITEMS:]:
				parts = [f"- {it.get('op', 'create')} {it.get('doctype', '?')}"]
				if it.get("name"):
					parts.append(f'"{it["name"]}"')
				if it.get("on"):
					parts.append(f"on {it['on']}")
				lines.append(" ".join(parts))

		if self.clarifications:
			lines.append("User decisions:")
			for c in self.clarifications[-_MAX_CLARIFICATIONS:]:
				lines.append(f"- Q: {c['q']}")
				lines.append(f"  A: {c['a']}")

		if self.insights_queries:
			lines.append("Recent Insights-mode questions (user asked about their site):")
			for iq in self.insights_queries[-_MAX_INSIGHTS_QUERIES:]:
				lines.append(f"- Q: {iq['q']}")
				lines.append(f"  A: {iq['a']}")

		if self.active_plan:
			title = self.active_plan.get("title") or "(untitled plan)"
			status = self.active_plan.get("status") or "proposed"
			lines.append(f"Active plan ({status}): {title}")
			summary = self.active_plan.get("summary")
			if summary:
				snippet = summary if len(summary) <= 300 else summary[:300] + "..."
				lines.append(f"  Summary: {snippet}")
			# If the plan is approved, surface the full step list so the
			# Dev pipeline's enhancer can treat the plan as an explicit
			# spec rather than a vague hint. "proposed" plans stay
			# abbreviated - the user hasn't committed yet.
			steps = self.active_plan.get("steps") or []
			if status == "approved" and steps:
				lines.append("  Approved plan steps (execute these verbatim):")
				for step in steps[:_MAX_PLAN_STEPS_RENDERED]:
					order = step.get("order", "?")
					action = step.get("action", "").strip()
					doctype = step.get("doctype")
					line = f"    {order}. {action}"
					if doctype:
						line += f" [{doctype}]"
					lines.append(line)
				if len(steps) > _MAX_PLAN_STEPS_RENDERED:
					lines.append(
						f"    ... ({len(steps) - _MAX_PLAN_STEPS_RENDERED} more steps)"
					)
			doctypes = self.active_plan.get("doctypes_touched") or []
			if doctypes and status == "approved":
				lines.append(
					f"  Doctypes the approved plan touches: {', '.join(doctypes)}"
				)

		if self.recent_prompts:
			lines.append("Recent prompts in this chat:")
			for p in self.recent_prompts[-_MAX_PROMPTS:]:
				snippet = p if len(p) <= 200 else p[:200] + "..."
				lines.append(f"- {snippet}")

		lines.append("=== END CONTEXT ===")
		return "\n".join(lines)

	def to_dict(self) -> dict[str, Any]:
		return {
			"conversation_id": self.conversation_id,
			"items": list(self.items),
			"clarifications": list(self.clarifications),
			"recent_prompts": list(self.recent_prompts),
			"insights_queries": list(self.insights_queries),
			"plan_documents": list(self.plan_documents),
			"active_plan": dict(self.active_plan) if self.active_plan else None,
			"updated_at": self.updated_at,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> ConversationMemory:
		raw_plan = data.get("active_plan")
		return cls(
			conversation_id=str(data.get("conversation_id", "")),
			items=list(data.get("items") or []),
			clarifications=list(data.get("clarifications") or []),
			recent_prompts=list(data.get("recent_prompts") or []),
			insights_queries=list(data.get("insights_queries") or []),
			plan_documents=list(data.get("plan_documents") or []),
			active_plan=dict(raw_plan) if isinstance(raw_plan, dict) else None,
			updated_at=float(data.get("updated_at") or time.time()),
		)


async def load_conversation_memory(
	store, site_id: str, conversation_id: str
) -> ConversationMemory:
	"""Load memory from Redis. Returns a fresh one if the store has nothing."""
	empty = ConversationMemory(conversation_id=conversation_id)
	if store is None or not site_id:
		return empty
	try:
		data = await store.get_task_state(site_id, _memory_key(conversation_id))
	except Exception as e:  # noqa: BLE001 — store-boundary contract (see test_load_tolerates_store_errors): any exception from the store must degrade to an empty memory, not crash the pipeline. Mocked stores in tests inject arbitrary exceptions (RuntimeError, etc.) to exercise this tolerance.
		logger.warning("conversation memory load failed for %s: %s", conversation_id, e)
		return empty
	if not data:
		return empty
	try:
		return ConversationMemory.from_dict(data)
	except (KeyError, TypeError, ValueError) as e:
		# from_dict can hit any of these on a malformed payload (missing
		# field, wrong type, bad enum value). Schema-evolution misses
		# show up here.
		logger.warning("conversation memory parse failed for %s: %s", conversation_id, e)
		return empty


async def save_conversation_memory(
	store, site_id: str, conversation_id: str, memory: ConversationMemory
) -> None:
	"""Persist memory to Redis. Best-effort - failures are logged, not raised."""
	if store is None or not site_id:
		return
	try:
		await store.set_task_state(site_id, _memory_key(conversation_id), memory.to_dict())
		logger.debug(
			"Saved conversation memory: site=%s, conversation=%s, items=%d",
			site_id, conversation_id, len(memory.items),
		)
	except Exception as e:  # noqa: BLE001 — store-boundary contract (see test_save_tolerates_store_errors): memory save is best-effort and must never block the pipeline. Mocked stores in tests inject arbitrary exceptions (RuntimeError, etc.) to exercise this.
		logger.warning(
			"conversation memory save failed for %s/%s: %s",
			site_id, conversation_id, e,
		)
