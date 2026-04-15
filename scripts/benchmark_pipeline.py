#!/usr/bin/env python3
"""Baseline benchmark harness for the Alfred agent pipeline.

Runs a fixed set of prompts through `_run_agent_pipeline` in-process (no live
WebSocket, no live Frappe site). Captures per-run metrics and writes a JSON
summary. Use this to measure pre/post impact of Phase 1 changes (tool cache,
Pydantic outputs, tool consolidation, etc.).

Usage:
    .venv/bin/python scripts/benchmark_pipeline.py
    .venv/bin/python scripts/benchmark_pipeline.py --tag phase1 --runs 3
    .venv/bin/python scripts/benchmark_pipeline.py --prompts 1 3 5

The script stubs out:
    - WebSocket (FakeWebSocket captures outbound messages for verification)
    - MCPClient (canned responses for common tool calls; dry-run always valid)
    - State store (no Redis - state persistence is a no-op)
    - Admin portal plan check (not configured -> skipped)
    - conn.ask_human (auto-replies "proceed with sensible defaults" so the
      clarifier doesn't block waiting for a real user)

Real components exercised:
    - Prompt sanitizer
    - enhance_prompt (real litellm call)
    - _clarify_requirements (real litellm call; responses auto-answered)
    - CrewAI full crew (real LLM calls for every agent)
    - _extract_changes / _rescue_regenerate_changeset / _dry_run_with_retry

Metrics captured per run:
    - wall_clock_seconds: end-to-end latency
    - llm_completion_count: number of litellm completion calls
    - llm_prompt_tokens: sum of prompt tokens across all calls
    - llm_completion_tokens: sum of completion tokens across all calls
    - llm_total_tokens: prompt + completion
    - mcp_tool_calls: total MCP tool invocations
    - mcp_tool_calls_by_name: per-tool breakdown
    - first_try_extraction: True if _extract_changes produced non-empty on
      first pass, before any rescue
    - rescue_triggered: True if the rescue regeneration path ran
    - dry_run_retries: count of dry-run self-heal retries
    - changeset_items: count of items in the final changeset
    - changeset_valid: True if the final dry-run passed
    - error: str or None
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

# Load .env before importing alfred / litellm so LLM config is honored
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
	for line in _env_file.read_text().splitlines():
		line = line.strip()
		if line and not line.startswith("#") and "=" in line:
			key, _, val = line.partition("=")
			os.environ.setdefault(key.strip(), val.strip())

# Silence CrewAI telemetry network calls - they slow down benchmarks and leak
# run info to an external endpoint we don't care about for local benchmarking.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")

# Repo root on the path so `import alfred.*` works from the scripts/ dir
sys.path.insert(0, str(Path(__file__).parent.parent))

import litellm  # noqa: E402


# ── Fixed benchmark prompt set ───────────────────────────────────────

BENCHMARK_PROMPTS = [
	{
		"id": 1,
		"name": "notification_approval_flow",
		"prompt": "Create a notification that emails the expense approver when a new expense claim is submitted",
		"notes": "Tests the approval notification pattern. Known tricky case - "
		"earlier agents drifted into prose mode on this prompt.",
	},
	{
		"id": 2,
		"name": "custom_field_simple",
		"prompt": "Add a priority custom field (Select type) to Sales Order with values Low, Medium, High",
		"notes": "Tests the custom_field_on_existing_doctype pattern. Single-item changeset.",
	},
	{
		"id": 3,
		"name": "new_doctype_basic",
		"prompt": (
			"Create a new doctype called Training Program with fields: program_name (Data, required), "
			"duration_days (Int), trainer (Link to Employee)"
		),
		"notes": "Tests basic new DocType creation with explicit field spec.",
	},
	{
		"id": 4,
		"name": "notification_different_domain",
		"prompt": "Create a notification that emails the sales manager when an Opportunity is marked as lost",
		"notes": "Different domain than the approval pattern. Catches hardcoded-bias issues.",
	},
	{
		"id": 5,
		"name": "server_script_validation",
		"prompt": "Create a server script that validates Leave Application from_date is not in the past",
		"notes": "Tests the validation_server_script pattern. Requires permission check.",
	},
	{
		"id": 6,
		"name": "audit_log",
		"prompt": "Create a server script that logs every change to Customer records to an audit log doctype",
		"notes": "Tests the audit_log_server_script pattern. Multi-item changeset.",
	},
]


# ── Canned MCP responses ─────────────────────────────────────────────
#
# These are minimal responses sufficient for the pipeline to proceed. They
# don't cover every tool call an agent might attempt; for unknown calls the
# mock returns a generic "not_found" error which the tool wrappers already
# surface as a tool failure without crashing the pipeline.

_CANNED_DOCTYPE_SCHEMAS = {
	"Expense Claim": {
		"name": "Expense Claim",
		"is_submittable": 1,
		"fields": [
			{"fieldname": "employee", "fieldtype": "Link", "options": "Employee", "reqd": 1},
			{"fieldname": "expense_approver", "fieldtype": "Link", "options": "User", "reqd": 0},
			{"fieldname": "approval_status", "fieldtype": "Select",
			 "options": "Draft\nApproved\nRejected"},
			{"fieldname": "total_claimed_amount", "fieldtype": "Currency"},
			{"fieldname": "company", "fieldtype": "Link", "options": "Company", "reqd": 1},
		],
	},
	"Leave Application": {
		"name": "Leave Application",
		"is_submittable": 1,
		"fields": [
			{"fieldname": "employee", "fieldtype": "Link", "options": "Employee", "reqd": 1},
			{"fieldname": "leave_approver", "fieldtype": "Link", "options": "User"},
			{"fieldname": "status", "fieldtype": "Select",
			 "options": "Open\nApproved\nRejected"},
			{"fieldname": "leave_type", "fieldtype": "Link", "options": "Leave Type", "reqd": 1},
			{"fieldname": "from_date", "fieldtype": "Date", "reqd": 1},
			{"fieldname": "to_date", "fieldtype": "Date", "reqd": 1},
		],
	},
	"Sales Order": {
		"name": "Sales Order",
		"is_submittable": 1,
		"fields": [
			{"fieldname": "customer", "fieldtype": "Link", "options": "Customer", "reqd": 1},
			{"fieldname": "transaction_date", "fieldtype": "Date", "reqd": 1},
			{"fieldname": "delivery_date", "fieldtype": "Date"},
			{"fieldname": "total", "fieldtype": "Currency"},
		],
	},
	"Opportunity": {
		"name": "Opportunity",
		"is_submittable": 0,
		"fields": [
			{"fieldname": "customer_name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "opportunity_from", "fieldtype": "Link", "options": "DocType"},
			{"fieldname": "status", "fieldtype": "Select",
			 "options": "Open\nQuotation\nConverted\nLost\nClosed"},
			{"fieldname": "sales_person", "fieldtype": "Link", "options": "Sales Person"},
			{"fieldname": "contact_by", "fieldtype": "Link", "options": "User"},
		],
	},
	"Customer": {
		"name": "Customer",
		"is_submittable": 0,
		"fields": [
			{"fieldname": "customer_name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "customer_type", "fieldtype": "Select"},
			{"fieldname": "territory", "fieldtype": "Link", "options": "Territory"},
		],
	},
	"Employee": {
		"name": "Employee",
		"is_submittable": 0,
		"fields": [
			{"fieldname": "employee_name", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "department", "fieldtype": "Link", "options": "Department"},
			{"fieldname": "reports_to", "fieldtype": "Link", "options": "Employee"},
		],
	},
	"Notification": {
		"name": "Notification",
		"is_submittable": 0,
		"fields": [
			{"fieldname": "subject", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "document_type", "fieldtype": "Link", "options": "DocType", "reqd": 1},
			{"fieldname": "event", "fieldtype": "Select",
			 "options": "New\nSave\nSubmit\nCancel\nDays After\nDays Before\nValue Change\nMethod\nCustom"},
			{"fieldname": "channel", "fieldtype": "Select",
			 "options": "Email\nSlack\nSystem Notification"},
			{"fieldname": "recipients", "fieldtype": "Table",
			 "options": "Notification Recipient"},
			{"fieldname": "message", "fieldtype": "Text Editor", "reqd": 1},
			{"fieldname": "condition", "fieldtype": "Code"},
			{"fieldname": "enabled", "fieldtype": "Check", "default": "1"},
		],
	},
	"Server Script": {
		"name": "Server Script",
		"is_submittable": 0,
		"fields": [
			{"fieldname": "script_type", "fieldtype": "Select",
			 "options": "DocType Event\nScheduler Event\nPermission Query\nAPI"},
			{"fieldname": "reference_doctype", "fieldtype": "Link", "options": "DocType"},
			{"fieldname": "doctype_event", "fieldtype": "Select",
			 "options": "Before Insert\nAfter Insert\nBefore Save\nAfter Save\nBefore Submit\nAfter Submit"},
			{"fieldname": "script", "fieldtype": "Code", "reqd": 1},
		],
	},
}


def _canned_doctype_schema(args: dict) -> dict:
	doctype = args.get("doctype", "")
	if doctype in _CANNED_DOCTYPE_SCHEMAS:
		return _CANNED_DOCTYPE_SCHEMAS[doctype]
	return {"error": "not_found", "message": f"DocType {doctype!r} not in benchmark fixtures"}


_CANNED_PATTERNS = {
	"approval_notification": {
		"description": "Email the approver when a document needs review BEFORE they click submit.",
		"category": "notification",
		"when_to_use": "recipient is the approver/reviewer who will submit the document",
		"event": "New",
		"event_reasoning": "approver IS the submitter; Submit would be a self-email",
		"template": {
			"op": "create", "doctype": "Notification",
			"data": {
				"doctype": "Notification",
				"event": "New", "channel": "Email",
				"recipients": [{"receiver_by_document_field": "<approver field>"}],
			},
		},
	},
	"post_approval_notification": {
		"description": "Email the requester or downstream team AFTER a document is approved.",
		"category": "notification",
		"when_to_use": "recipient is the requester or a team informed after approval",
		"event": "Submit",
	},
	"validation_server_script": {
		"description": "Block a document from saving unless a custom rule passes.",
		"category": "script",
		"when_to_use": "enforce a rule that can't be a simple field constraint",
	},
	"custom_field_on_existing_doctype": {
		"description": "Add a field to an existing DocType via Custom Field.",
		"category": "field",
		"when_to_use": "one new field on an existing DocType, not a new entity",
	},
	"audit_log_server_script": {
		"description": "Record every change to a DocType in a separate audit log.",
		"category": "script",
		"when_to_use": "compliance requires change history beyond last-modified",
	},
}


def _canned_lookup_doctype(args: dict) -> Any:
	name = args.get("name", "")
	layer = (args.get("layer") or "both").lower()
	schema = _CANNED_DOCTYPE_SCHEMAS.get(name)
	if schema is None:
		return {"error": "not_found", "message": f"DocType {name!r} not in benchmark fixtures"}
	framework_view = {
		"name": name,
		"app": "erpnext",
		"module": "Benchmark",
		"is_submittable": schema.get("is_submittable", 0),
		"fields": schema.get("fields", []),
		"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1}],
	}
	if layer == "framework":
		return framework_view
	if layer == "site":
		return schema
	return {
		"name": name,
		"framework": framework_view,
		"site": schema,
		"custom_fields": [],
	}


def _canned_lookup_pattern(args: dict) -> Any:
	query = (args.get("query") or "").lower()
	kind = (args.get("kind") or "all").lower()
	if kind == "list":
		return {"patterns": [
			{"name": n, "description": p.get("description", ""),
			 "category": p.get("category", ""), "when_to_use": p.get("when_to_use", "")}
			for n, p in _CANNED_PATTERNS.items()
		]}
	if kind == "name":
		entry = _CANNED_PATTERNS.get(query)
		if entry is None:
			return {"error": "not_found", "message": f"Pattern {query!r} not found"}
		return {"pattern": entry, "name": query}
	# search / all - simple substring match across name + description
	hits = [
		{"name": n, "description": p.get("description", ""),
		 "category": p.get("category", ""), "when_to_use": p.get("when_to_use", ""),
		 "_score": 1}
		for n, p in _CANNED_PATTERNS.items()
		if any(term in n.lower() or term in p.get("description", "").lower()
			   or term in p.get("when_to_use", "").lower()
			   for term in query.split())
	]
	if kind == "all" and query in _CANNED_PATTERNS:
		return {"pattern": _CANNED_PATTERNS[query], "name": query, "source": "exact"}
	return {"doctypes": [], "patterns": hits[:5], "source": "search"}


def _canned_response(tool_name: str, args: dict) -> Any:
	"""Return canned response for a benchmark MCP call.

	Returns dict for real responses, or dict {error, message} for unknowns
	(which matches the `_safe_execute` wrapper's shape).
	"""
	if tool_name == "lookup_doctype":
		return _canned_lookup_doctype(args)
	if tool_name == "lookup_pattern":
		return _canned_lookup_pattern(args)
	if tool_name == "get_doctype_schema":
		return _canned_doctype_schema(args)
	if tool_name == "get_doctypes":
		return {"doctypes": list(_CANNED_DOCTYPE_SCHEMAS.keys())}
	if tool_name == "get_site_info":
		return {
			"version": "15.x-benchmark",
			"installed_apps": [
				{"name": "frappe", "version": "15.0.0"},
				{"name": "erpnext", "version": "15.0.0"},
				{"name": "hrms", "version": "15.0.0"},
			],
			"site": "benchmark.local",
		}
	if tool_name == "get_existing_customizations":
		return {"custom_fields": [], "server_scripts": [], "client_scripts": []}
	if tool_name == "get_user_context":
		return {"user": "benchmark@local", "roles": ["System Manager"], "enabled": 1}
	if tool_name == "check_permission":
		return {"permitted": True, "reason": "benchmark fixture - all permissions granted"}
	if tool_name == "validate_name_available":
		return {"available": True}
	if tool_name == "has_active_workflow":
		return {"has_workflow": False}
	if tool_name == "check_has_records":
		return {"has_records": False, "count": 0}
	if tool_name == "dry_run_changeset":
		# For baseline measurement we assume all changesets pass dry-run.
		# Phase 1 changes are about token/latency, not validation accuracy.
		changes = args.get("changes", [])
		if isinstance(changes, str):
			try:
				changes = json.loads(changes)
			except Exception:
				changes = []
		return {
			"valid": True,
			"issues": [],
			"validated": len(changes) if isinstance(changes, list) else 0,
		}
	return {"error": "not_found", "message": f"tool {tool_name!r} not stubbed in benchmark"}


# ── Metrics collection ──────────────────────────────────────────────


class BenchmarkMetrics:
	"""Per-run metric accumulator. Reset between prompts.

	LLM call tracking: litellm's `success_callback` fires once per stream chunk
	for streamed responses. To count completions not chunks, we dedupe by the
	response object's `.id` field. Token counts are taken from the LAST
	observed usage per id (usage is cumulative over chunks but only the final
	chunk reports a non-zero total for some models).
	"""

	def __init__(self):
		self.start_wall: float = 0.0
		self.end_wall: float = 0.0
		# Per-completion tokens keyed by response id. Last seen usage wins so
		# we capture the final cumulative totals, not partial chunks.
		self._completion_tokens: dict[str, tuple[int, int]] = {}
		# Callback invocation count - useful for diagnosing streaming behavior
		self.llm_callback_invocations: int = 0
		self.mcp_tool_calls: int = 0
		self.mcp_tool_calls_by_name: dict[str, int] = {}
		self.rescue_triggered: bool = False
		self.first_try_extraction: bool | None = None
		self.dry_run_retries: int = 0
		self.changeset_items: int = 0
		self.changeset_valid: bool = False
		self.error: str | None = None
		self.outbound_messages: list[dict] = []

	def record_mcp_call(self, tool_name: str):
		self.mcp_tool_calls += 1
		self.mcp_tool_calls_by_name[tool_name] = self.mcp_tool_calls_by_name.get(tool_name, 0) + 1

	def record_llm(self, response_id: str, prompt_tokens: int, completion_tokens: int):
		self.llm_callback_invocations += 1
		if response_id:
			# Overwrite with latest totals for this id - avoids double-counting
			# across streamed chunks.
			self._completion_tokens[response_id] = (prompt_tokens, completion_tokens)
		else:
			# No id - fall back to append with a synthetic key so we don't lose
			# the count but can still dedupe by presence.
			fallback_id = f"noid-{self.llm_callback_invocations}"
			self._completion_tokens[fallback_id] = (prompt_tokens, completion_tokens)

	@property
	def llm_completion_count(self) -> int:
		return len(self._completion_tokens)

	@property
	def llm_prompt_tokens(self) -> int:
		return sum(p for p, _ in self._completion_tokens.values())

	@property
	def llm_completion_tokens(self) -> int:
		return sum(c for _, c in self._completion_tokens.values())

	def to_dict(self) -> dict:
		return {
			"wall_clock_seconds": round(self.end_wall - self.start_wall, 2) if self.end_wall else None,
			"llm_completion_count": self.llm_completion_count,
			"llm_callback_invocations": self.llm_callback_invocations,
			"llm_prompt_tokens": self.llm_prompt_tokens,
			"llm_completion_tokens": self.llm_completion_tokens,
			"llm_total_tokens": self.llm_prompt_tokens + self.llm_completion_tokens,
			"mcp_tool_calls": self.mcp_tool_calls,
			"mcp_tool_calls_by_name": dict(self.mcp_tool_calls_by_name),
			"dedup_hits": getattr(self, "dedup_hits", 0),
			"phase1_budget_exceeded": getattr(self, "phase1_budget_exceeded", False),
			"phase1_failure_count": getattr(self, "phase1_failure_count", 0),
			"first_try_extraction": self.first_try_extraction,
			"rescue_triggered": self.rescue_triggered,
			"dry_run_retries": self.dry_run_retries,
			"changeset_items": self.changeset_items,
			"changeset_valid": self.changeset_valid,
			"error": self.error,
			"outbound_message_count": len(self.outbound_messages),
		}


# ── litellm token tracking ──────────────────────────────────────────

_ACTIVE_METRICS: BenchmarkMetrics | None = None


def _litellm_success_callback(kwargs, completion_response, start_time, end_time):
	"""Called by litellm after every successful completion (or per chunk for streamed)."""
	global _ACTIVE_METRICS
	if _ACTIVE_METRICS is None:
		return
	try:
		response_id = getattr(completion_response, "id", "") or ""
		usage = getattr(completion_response, "usage", None)
		if usage is None:
			# Streamed intermediate chunk with no usage info - skip but count the invocation.
			_ACTIVE_METRICS.llm_callback_invocations += 1
			return
		if hasattr(usage, "prompt_tokens"):
			p = int(usage.prompt_tokens or 0)
			c = int(usage.completion_tokens or 0)
		else:
			p = int(usage.get("prompt_tokens", 0) or 0)
			c = int(usage.get("completion_tokens", 0) or 0)
		_ACTIVE_METRICS.record_llm(response_id, p, c)
	except Exception as e:
		print(f"[litellm callback] failed to record tokens: {e}", file=sys.stderr)


litellm.success_callback = [_litellm_success_callback]


# ── Mock ConnectionState + WebSocket + MCPClient ────────────────────


class FakeWebSocket:
	"""Minimal stand-in for FastAPI's WebSocket in the benchmark harness."""

	def __init__(self, app_state: SimpleNamespace):
		self.app = SimpleNamespace(state=app_state)
		self.sent: list[dict] = []
		self.client_state = "connected"

	async def send_json(self, message: dict):
		self.sent.append(message)

	async def close(self, code: int = 1000, reason: str = ""):
		self.client_state = "closed"


class MockMCPClient:
	"""MCPClient substitute - returns canned responses, records call counts."""

	def __init__(self, metrics: BenchmarkMetrics, main_loop: asyncio.AbstractEventLoop):
		self.metrics = metrics
		self._main_loop = main_loop
		self._timeout = 30
		self._on_call = None

	async def call_tool(self, tool_name: str, arguments: dict | None = None) -> Any:
		self.metrics.record_mcp_call(tool_name)
		# Simulate a small round-trip delay (1ms) so asyncio schedules other tasks
		await asyncio.sleep(0.001)
		return _canned_response(tool_name, arguments or {})

	def call_sync(self, tool_name: str, arguments: dict | None = None, timeout: int | None = None) -> Any:
		"""Sync version used by CrewAI tool wrappers from worker threads."""
		self.metrics.record_mcp_call(tool_name)
		return _canned_response(tool_name, arguments or {})

	def handle_response(self, message: dict):
		pass  # no-op in benchmark


def build_mock_conn(
	metrics: BenchmarkMetrics, main_loop: asyncio.AbstractEventLoop,
) -> Any:
	"""Construct a SimpleNamespace that looks enough like ConnectionState."""
	from alfred.config import get_settings

	app_state = SimpleNamespace(
		settings=get_settings(),
		redis=None,
	)
	ws = FakeWebSocket(app_state)

	# Get the real site_config shape from the settings + fallback LLM config
	site_config = {
		"site_id": "benchmark.local",
		"llm_provider": "ollama",
		"llm_model": os.environ.get("FALLBACK_LLM_MODEL", "ollama/qwen2.5-coder:32b"),
		"llm_api_key": os.environ.get("FALLBACK_LLM_API_KEY", ""),
		"llm_base_url": os.environ.get("FALLBACK_LLM_BASE_URL", ""),
		"llm_max_tokens": 4096,
		"llm_temperature": 0.1,
		"llm_num_ctx": 8192,
		"pipeline_mode": "full",
		"max_retries_per_agent": 2,
		"max_tasks_per_user_per_hour": 100,
		"task_timeout_seconds": 900,
		"enable_auto_deploy": False,
	}

	mock_mcp = MockMCPClient(metrics, main_loop)

	conn = SimpleNamespace(
		websocket=ws,
		site_id="benchmark.local",
		user="benchmark@local",
		roles=["System Manager"],
		site_config=site_config,
		last_acked_msg_id=None,
		pending_acks={},
		_pending_questions={},
		mcp_client=mock_mcp,
		active_pipeline=None,
	)

	# Track outbound messages in metrics
	async def send_tracked(message: dict):
		metrics.outbound_messages.append(message)
		await ws.send_json(message)

	conn.send = send_tracked

	# Auto-answer any clarification questions with a sensible default
	async def auto_ask_human(question: str, choices: list[str] | None = None, timeout: int = 900) -> str:
		if choices:
			return choices[0]
		# Generic defaults biased toward "minimum viable" answers
		low = question.lower()
		if "field" in low and "which" in low:
			return "use the first link field that matches semantically"
		if "event" in low:
			return "trigger on New (document creation)"
		return "proceed with sensible defaults"

	conn.ask_human = auto_ask_human
	conn.resolve_question = lambda msg_id, answer: None

	return conn


# ── Benchmark runner ────────────────────────────────────────────────


async def run_single_prompt(prompt_entry: dict) -> dict:
	"""Run one pipeline invocation and return metrics + result summary."""
	global _ACTIVE_METRICS
	metrics = BenchmarkMetrics()
	_ACTIVE_METRICS = metrics

	# Import lazily so the litellm callback is set before alfred imports trigger
	# any LLM calls themselves (e.g. at module init)
	from alfred.api.websocket import _run_agent_pipeline, _extract_changes

	conn = build_mock_conn(metrics, asyncio.get_running_loop())

	# Hook _extract_changes so we can detect whether the first-pass parse
	# succeeded before any rescue path fired. We wrap the imported symbol in
	# alfred.api.websocket via monkey-patching a module attribute.
	import alfred.api.websocket as ws_mod
	original_extract = ws_mod._extract_changes
	original_rescue = ws_mod._rescue_regenerate_changeset

	first_try_holder = {"recorded": False, "value": None}

	def hooked_extract(result_text):
		result = original_extract(result_text)
		if not first_try_holder["recorded"]:
			first_try_holder["value"] = bool(result)
			first_try_holder["recorded"] = True
		return result

	async def hooked_rescue(original_prompt, failed_output, site_config, event_callback):
		metrics.rescue_triggered = True
		return await original_rescue(original_prompt, failed_output, site_config, event_callback)

	ws_mod._extract_changes = hooked_extract
	ws_mod._rescue_regenerate_changeset = hooked_rescue

	metrics.start_wall = time.monotonic()
	try:
		await _run_agent_pipeline(conn, f"benchmark-conv-{prompt_entry['id']}", prompt_entry["prompt"])
	except Exception as e:
		metrics.error = f"{type(e).__name__}: {e}"
	finally:
		metrics.end_wall = time.monotonic()
		ws_mod._extract_changes = original_extract
		ws_mod._rescue_regenerate_changeset = original_rescue
		_ACTIVE_METRICS = None

	metrics.first_try_extraction = first_try_holder["value"]

	# Capture Phase 1 run_state metrics if present
	run_state = getattr(conn.mcp_client, "run_state", None)
	if run_state:
		metrics.dedup_hits = run_state.get("dedup_hits", 0)
		metrics.phase1_failure_count = run_state.get("failure_count", 0)
		# Budget exceeded is implicit from the failure list
		metrics.phase1_budget_exceeded = any(
			f[1] == "budget_exceeded" for f in run_state.get("failures", [])
		)

	# Parse the outbound message stream to extract the final changeset
	for msg in metrics.outbound_messages:
		mtype = msg.get("type")
		data = msg.get("data", {}) or {}
		if mtype == "changeset":
			changes = data.get("changes") or []
			metrics.changeset_items = len(changes) if isinstance(changes, list) else 0
			dry_run = data.get("dry_run") or {}
			metrics.changeset_valid = bool(dry_run.get("valid", True))

	result = metrics.to_dict()
	result["prompt_id"] = prompt_entry["id"]
	result["prompt_name"] = prompt_entry["name"]
	result["prompt"] = prompt_entry["prompt"]
	return result


async def run_benchmarks(prompt_ids: list[int], runs: int) -> dict:
	prompts = [p for p in BENCHMARK_PROMPTS if p["id"] in prompt_ids]
	all_results: list[dict] = []

	for prompt_entry in prompts:
		for run_index in range(runs):
			print(f"\n=== Running prompt {prompt_entry['id']}/{prompt_entry['name']} "
				  f"(iteration {run_index + 1}/{runs}) ===", flush=True)
			result = await run_single_prompt(prompt_entry)
			result["run_index"] = run_index
			all_results.append(result)
			_print_run_summary(result)

	return {
		"timestamp": datetime.now().isoformat(timespec="seconds"),
		"model": os.environ.get("FALLBACK_LLM_MODEL", "unknown"),
		"base_url": os.environ.get("FALLBACK_LLM_BASE_URL", ""),
		"runs_per_prompt": runs,
		"results": all_results,
		"summary": _aggregate(all_results),
	}


def _print_run_summary(result: dict):
	print(f"  wall={result['wall_clock_seconds']}s "
		  f"tokens={result['llm_total_tokens']} "
		  f"llm_calls={result['llm_completion_count']} "
		  f"mcp_calls={result['mcp_tool_calls']} "
		  f"first_try={result['first_try_extraction']} "
		  f"rescue={result['rescue_triggered']} "
		  f"items={result['changeset_items']} "
		  f"valid={result['changeset_valid']}"
		  + (f" ERROR={result['error']}" if result.get("error") else ""),
		  flush=True)


def _aggregate(results: list[dict]) -> dict:
	if not results:
		return {}
	# A run is "executed" if it actually reached the crew (LLM calls > 0).
	# Runs that got 0 activity were blocked by the sanitizer / plan check.
	executed = [r for r in results if (r.get("llm_completion_count") or 0) > 0]
	aborted = [r for r in results if (r.get("llm_completion_count") or 0) == 0]
	errored = [r for r in results if r.get("error")]

	total = len(results)
	ok = len(executed)
	first_try_ok = sum(1 for r in executed if r.get("first_try_extraction"))
	rescue_count = sum(1 for r in executed if r.get("rescue_triggered"))

	def avg(key: str) -> float:
		vals = [r[key] for r in executed if r.get(key) is not None]
		return round(sum(vals) / len(vals), 2) if vals else 0.0

	return {
		"total_runs": total,
		"executed_runs": ok,
		"aborted_runs": len(aborted),
		"aborted_prompt_ids": [r["prompt_id"] for r in aborted],
		"errored_runs": len(errored),
		"avg_wall_clock_seconds": avg("wall_clock_seconds"),
		"avg_llm_total_tokens": avg("llm_total_tokens"),
		"avg_llm_completion_count": avg("llm_completion_count"),
		"avg_mcp_tool_calls": avg("mcp_tool_calls"),
		"first_try_success_count": first_try_ok,
		"first_try_success_rate": round(first_try_ok / ok, 2) if ok else 0,
		"rescue_triggered_count": rescue_count,
		"rescue_triggered_rate": round(rescue_count / ok, 2) if ok else 0,
	}


def main():
	parser = argparse.ArgumentParser(description="Alfred pipeline benchmark harness")
	parser.add_argument(
		"--tag", default="baseline",
		help="Output filename tag (default: baseline). Produces benchmarks/<tag>_<date>.json",
	)
	parser.add_argument(
		"--runs", type=int, default=1,
		help="Runs per prompt (default 1). Higher values reduce variance.",
	)
	parser.add_argument(
		"--prompts", type=int, nargs="+",
		help="Prompt IDs to run (default: all). e.g. --prompts 1 3 5",
	)
	args = parser.parse_args()

	prompt_ids = args.prompts or [p["id"] for p in BENCHMARK_PROMPTS]

	print(f"Alfred benchmark harness")
	print(f"  Model: {os.environ.get('FALLBACK_LLM_MODEL', 'unknown')}")
	print(f"  Base URL: {os.environ.get('FALLBACK_LLM_BASE_URL', '(provider default)')}")
	print(f"  Prompts: {prompt_ids}")
	print(f"  Runs per prompt: {args.runs}")

	report = asyncio.run(run_benchmarks(prompt_ids, args.runs))

	out_dir = Path(__file__).parent.parent / "benchmarks"
	out_dir.mkdir(exist_ok=True)
	date = datetime.now().strftime("%Y-%m-%d")
	out_path = out_dir / f"{args.tag}_{date}.json"
	out_path.write_text(json.dumps(report, indent=2))

	print(f"\n=== Summary ===")
	s = report["summary"]
	print(f"  Total runs:           {s.get('total_runs', 0)}")
	print(f"  Executed runs:        {s.get('executed_runs', 0)}")
	if s.get("aborted_runs", 0):
		print(f"  Aborted runs:         {s.get('aborted_runs', 0)} (prompt ids: {s.get('aborted_prompt_ids', [])})")
	if s.get("errored_runs", 0):
		print(f"  Errored runs:         {s.get('errored_runs', 0)}")
	print(f"  Avg wall-clock:       {s.get('avg_wall_clock_seconds', 0)}s")
	print(f"  Avg LLM tokens:       {s.get('avg_llm_total_tokens', 0)}")
	print(f"  Avg LLM calls:        {s.get('avg_llm_completion_count', 0)}")
	print(f"  Avg MCP calls:        {s.get('avg_mcp_tool_calls', 0)}")
	print(f"  First-try accuracy:   {s.get('first_try_success_rate', 0) * 100:.0f}%")
	print(f"  Rescue rate:          {s.get('rescue_triggered_rate', 0) * 100:.0f}%")
	print(f"\nReport written to: {out_path}")


if __name__ == "__main__":
	main()
