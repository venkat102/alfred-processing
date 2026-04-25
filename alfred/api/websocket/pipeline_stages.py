"""Pipeline stages invoked from the WebSocket handler (TD-H2 split from
``alfred/api/websocket.py``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import TYPE_CHECKING

from alfred.api.websocket.extract import _extract_changes

if TYPE_CHECKING:
	from alfred.agents.crew import CrewState
	from alfred.api.websocket.connection import ConnectionState

logger = logging.getLogger("alfred.websocket")


async def _dry_run_with_retry(
	conn: ConnectionState,
	state: CrewState,
	changes: list[dict],
	site_config: dict,
	event_callback,
) -> dict:
	"""Run dry-run validation on a changeset. On failure, self-heal once by
	re-running just the Developer agent with the issues as context.

	Returns a dict shaped like:
		{
			"valid": bool,
			"status": "ok" | "invalid" | "infra_error" | "skipped",
			"issues": list[dict],
			"validated": int,
			"_final_changes": list[dict]   # the changeset to actually show the user
		}

	``status`` distinguishes three cases the UI must render differently:

	  - ``ok``         - validation ran, changeset is valid. Deploy safe.
	  - ``invalid``    - validation ran, changeset has real content issues.
	                     UI shows the issue list; user can approve anyway
	                     but knows what's wrong.
	  - ``infra_error`` - validation DID NOT run (MCP call failed, permission
	                     denied, Frappe internal error). UI MUST NOT treat
	                     this as "changeset is fine" - the user is flying
	                     blind if they approve. Recommended UI: disable the
	                     Approve button and show a retry-or-cancel banner.
	  - ``skipped``    - no MCP client on the connection (dev without a bench).

	Never raises - on any failure the function returns a best-effort dict
	carrying the right ``status`` flag.
	"""
	if not conn.mcp_client:
		logger.info("Skipping dry-run: no MCP client on connection")
		return {
			"valid": True, "status": "skipped",
			"issues": [], "validated": 0, "_final_changes": changes,
		}

	async def _run(changeset):
		"""Call the dry_run_changeset MCP tool and normalize the response.

		Tags each return with ``status`` so the caller can distinguish
		"MCP gave us a genuine failure verdict" (``status=invalid``) from
		"we never actually validated" (``status=infra_error``). Before
		this split the UI saw the same ``valid=False`` shape for both and
		could silently greenlight a deploy past an infra failure.
		"""
		try:
			result = await conn.mcp_client.call_tool(
				"dry_run_changeset", {"changes": changeset}
			)
			if not isinstance(result, dict):
				return {
					"valid": False, "status": "infra_error", "validated": 0,
					"issues": [{
						"severity": "warning",
						"issue": f"dry_run_changeset returned unexpected type: {type(result).__name__}",
					}],
				}
			# The client-side _safe_execute wrapper returns {"error": "...", "message": "..."}
			# on permission denied / not found / internal error. Infra failure,
			# not a content issue.
			if result.get("error"):
				return {
					"valid": False, "status": "infra_error", "validated": 0,
					"issues": [{
						"severity": "warning",
						"issue": f"Validation could not run: {result.get('message', result['error'])}",
					}],
				}
			# Genuine validator verdict - may be valid=True (ok) or valid=False
			# with content issues (invalid).
			result["status"] = "ok" if result.get("valid") else "invalid"
			return result
		except Exception as e:  # noqa: BLE001 — pre-existing master broad catch (best-effort path; revisit in TD-H3 follow-up)
			logger.warning("Dry-run MCP call failed: %s", e)
			return {
				"valid": False, "status": "infra_error", "validated": 0,
				"issues": [{"severity": "warning", "issue": f"Validation infrastructure error: {e}"}],
			}

	await event_callback("validation", {
		"agent": "Validator", "status": "dry_run_running",
		"message": "Validating changeset against live site...",
	})

	dry_run = await _run(changes)

	if dry_run.get("valid") or state.dry_run_retries >= 1:
		dry_run["_final_changes"] = changes
		return dry_run

	# Self-heal: re-run the Developer agent once with the validation issues injected
	state.dry_run_retries = 1
	issues_list = dry_run.get("issues", [])
	issues_json = json.dumps(issues_list, indent=2)
	changes_json = json.dumps(changes, indent=2)

	# Detect the "Server Script used `import`" flavour of failure and surface
	# a specific banner so the retry doesn't treat it as a generic issue.
	# The failure is deterministic (RestrictedPython's Import_ guard) and the
	# fix is mechanical (drop the import line, use pre-bound names), so a
	# targeted reminder lets the model converge in one shot.
	import_violation = any(
		isinstance(issue, dict)
		and "import" in str(issue.get("issue", "")).lower()
		and "server script" in str(issue.get("issue", "")).lower()
		for issue in issues_list
	)
	import_banner = ""
	if import_violation:
		import_banner = (
			"\n=== SERVER SCRIPT IMPORT VIOLATION (read this first) ===\n"
			"Your previous Server Script contains an `import` statement.\n"
			"Frappe Server Scripts run under RestrictedPython, which bans `import`\n"
			"at compile time. Remove EVERY import line and use the pre-bound\n"
			"names directly:\n"
			"  - `json`      -> json.loads(s), json.dumps(obj)\n"
			"  - `datetime`  -> datetime.datetime, datetime.date, datetime.timedelta\n"
			"  - dates       -> frappe.utils.nowdate(), frappe.utils.now_datetime(),\n"
			"                   frappe.utils.getdate(x), frappe.utils.add_days(d, n),\n"
			"                   frappe.utils.date_diff(a, b)\n"
			"  - `requests`  -> frappe.make_get_request(url) / frappe.make_post_request(url, data=...)\n"
			"  - numbers     -> frappe.utils.flt(x), frappe.utils.cint(x)\n"
			"The retry MUST NOT contain any `import` anywhere in the Server Script.\n"
			"=========================================================\n\n"
		)

	await event_callback("validation", {
		"agent": "Developer", "status": "dry_run_retry",
		"message": f"Dry-run found {len(dry_run.get('issues', []))} issue(s). Asking Developer to fix...",
	})

	try:
		from crewai import Crew, Process, Task

		from alfred.agents.definitions import build_agents
		from alfred.tools.mcp_tools import build_mcp_tools

		custom_tools = build_mcp_tools(conn.mcp_client)
		agents = build_agents(site_config=site_config, custom_tools=custom_tools)
		developer = agents["developer"]
		# Cap the fix-task iterations so qwen-style repeat loops can't wedge
		# the pipeline. If it can't fix it in 3 ReAct steps, escalate to the
		# user rather than spending more tokens spinning.
		developer.max_iter = 3

		fix_task = Task(
			description=(
				"OUTPUT FORMAT (STRICT): Your entire Final Answer MUST be a single JSON\n"
				"array. Start with `[` and end with `]`. Do NOT repeat the array. Do NOT\n"
				"include markdown code fences. Do NOT include any prose, commentary, or\n"
				"duplicate copies. If you have nothing to change, output the input array\n"
				"unchanged - still as a single clean array.\n\n"
				f"{import_banner}"
				"The changeset you produced failed dry-run validation against the live site. "
				"Fix the issues below and produce a corrected changeset.\n\n"
				f"Validation issues:\n{issues_json}\n\n"
				f"Original changeset:\n{changes_json}\n\n"
				"Return a corrected changeset as a JSON array of complete document definitions. "
				"Every 'data' object must include all required fields for its doctype. "
				"Use lookup_doctype(name, layer='framework') to verify field names before writing."
			),
			expected_output="A JSON array of corrected changeset items (single array, no repeats).",
			agent=developer,
		)

		fix_crew = Crew(
			agents=[developer],
			tasks=[fix_task],
			process=Process.sequential,
			memory=False,
			verbose=False,
		)

		loop = asyncio.get_running_loop()
		retry_result = await loop.run_in_executor(None, fix_crew.kickoff)
		retry_text = str(retry_result) if retry_result else ""
		fixed_changes = _extract_changes(retry_text)

		if fixed_changes:
			changes = fixed_changes
			dry_run = await _run(fixed_changes)
		else:
			await event_callback("validation", {
				"agent": "Developer", "status": "dry_run_retry_empty",
				"message": "Retry produced no parseable changeset. Showing original issues.",
			})
	except Exception as e:
		logger.error("Dry-run retry failed: %s", e, exc_info=True)
		await event_callback("validation", {
			"agent": "Developer", "status": "dry_run_retry_failed",
			"message": f"Retry crashed: {e}. Showing original issues.",
		})
		# Keep original changes + original dry_run issues

	dry_run["_final_changes"] = changes
	return dry_run


async def _clarify_requirements(
	enhanced_prompt: str,
	conn: ConnectionState,
	event_callback,
) -> tuple[str, list[tuple[str, str]]]:
	"""Structured clarification gate: ask the user about ambiguities before the crew runs.

	One direct LLM call asks "what ambiguities exist in this request that only
	a human can resolve?" and returns a JSON list of questions. If non-empty, we
	send each question to the UI via conn.ask_human() (which blocks until the
	user responds), then append the Q/A pairs to the enhanced prompt so the
	downstream agents have the clarified context.

	Principles:
	- Only blocking decisions (schema choices, trigger events, scope) warrant
	  a question. Not implementation details the agents can figure out.
	- Max 3 questions per pass to avoid interrogation fatigue.
	- Short timeout on each question (15 min) - if the user walks away, the
	  pipeline still completes using the LLM's best guess.
	- Never raises: on any error, returns the original prompt unchanged.

	Returns:
		Tuple of (clarified_prompt, qa_pairs). qa_pairs is the list of
		(question, answer) tuples so the caller can persist them into the
		conversation memory for future turns.
	"""
	if not enhanced_prompt:
		return enhanced_prompt, []

	try:
		from alfred.llm_client import ollama_chat

		site_config = conn.site_config or {}

		system = (
			"You are a Frappe requirements analyst. You receive an enhanced user "
			"request and must identify BLOCKING ambiguities that only a human can "
			"resolve before any code is generated.\n\n"
			"A BLOCKING ambiguity is one where the wrong choice would force a rework. "
			"The categories you should look for:\n"
			"- Trigger timing: when should the customization fire? (on create / on save / "
			"on submit / on field change / scheduled - the wrong choice produces unwanted "
			"or missing events)\n"
			"- Recipient target: which user or role should receive a notification? When "
			"the request says 'the manager' or 'the approver', which specific Link field "
			"on the target DocType holds that user?\n"
			"- Scope: which DocType(s) does the customization apply to? When the "
			"user uses a category word ('orders', 'invoices', 'requests'), it may "
			"map to more than one Frappe DocType; flag the ambiguity so the user "
			"can name the exact target DocType.\n"
			"- Permissions: 'only X can do Y' - which exact role or permission?\n\n"
			"A NON-BLOCKING detail (do NOT ask about these):\n"
			"- Field labels / naming conventions (agent can decide)\n"
			"- Code style / implementation choices (agent can decide)\n"
			"- Stylistic UI wording (agent can decide)\n\n"
			"RULES:\n"
			"- Ask at most 3 questions. Usually zero is the right answer.\n"
			"- If the request is unambiguous, return an empty array.\n"
			"- Each question must be answerable in one short sentence.\n"
			"- Include a `choices` array with 2-4 concrete options when the answer "
			"has a finite set; omit or use [] if open-ended.\n\n"
			"OUTPUT FORMAT (STRICT): a raw JSON array, no prose, no fences:\n"
			'[{"question": "...", "choices": ["option A", "option B"]}, ...]\n'
			"If nothing to ask, output exactly `[]`."
		)

		await event_callback("clarify", {
			"agent": "Requirements", "status": "checking",
			"message": "Checking for ambiguities that need your input...",
		})

		raw = await ollama_chat(
			messages=[
				{"role": "system", "content": system},
				{"role": "user", "content": f"ENHANCED REQUEST:\n{enhanced_prompt[:6000]}"},
			],
			site_config=site_config,
			tier="reasoning",
			max_tokens=1024,
			temperature=0.0,
			num_ctx_override=8192,
			timeout=int(site_config.get("llm_timeout") or 60),
		)
		# Clarify LLM output is derived from the user's prompt and typically
		# contains user context (field names, entity names, values). Logging
		# 500 chars at INFO leaks that content; keep length at INFO for
		# dashboard activity, verbatim at DEBUG for local troubleshooting.
		logger.info("Clarify pass result: chars=%d", len(raw or ""))
		logger.debug("Clarify pass result (first 500): %r", (raw or "")[:500])

		questions = []
		try:
			cleaned = re.sub(r'^```(?:json)?\s*', '', (raw or "").strip())
			cleaned = re.sub(r'\s*```$', '', cleaned)
			match = re.search(r'\[.*\]', cleaned, re.DOTALL)
			if match:
				parsed = json.loads(match.group())
				if isinstance(parsed, list):
					questions = [q for q in parsed if isinstance(q, dict) and q.get("question")]
		except Exception as e:  # noqa: BLE001 — pre-existing master broad catch (best-effort path; revisit in TD-H3 follow-up)
			logger.warning("Clarify pass: failed to parse questions: %s", e)
			questions = []

		if not questions:
			logger.info("Clarify pass: no blocking ambiguities - proceeding directly")
			return enhanced_prompt, []

		questions = questions[:3]
		logger.info("Clarify pass: asking %d question(s)", len(questions))

		qa_pairs = []
		for q in questions:
			question_text = str(q.get("question", "")).strip()
			if not question_text:
				continue
			raw_choices = q.get("choices") or []
			choices = [str(c).strip() for c in raw_choices if str(c).strip()][:4]

			await event_callback("clarify", {
				"agent": "Requirements", "status": "asking",
				"message": question_text,
			})
			try:
				answer = await conn.ask_human(question_text, choices=choices, timeout=900)
			except Exception as e:  # noqa: BLE001 — pre-existing master broad catch (best-effort path; revisit in TD-H3 follow-up)
				logger.warning("ask_human failed for question %r: %s", question_text, e)
				answer = "[no response]"

			qa_pairs.append((question_text, answer or "[no response]"))

		if not qa_pairs:
			return enhanced_prompt, []

		clarifications_block = "\n\nUSER CLARIFICATIONS:\n" + "\n".join(
			f"Q: {q}\nA: {a}" for q, a in qa_pairs
		)
		clarified = enhanced_prompt + clarifications_block

		await event_callback("clarify", {
			"agent": "Requirements", "status": "done",
			"message": f"Got {len(qa_pairs)} clarification(s) - continuing with your input.",
		})

		return clarified, qa_pairs
	except Exception as e:  # noqa: BLE001 — pre-existing master broad catch (best-effort path; revisit in TD-H3 follow-up)
		logger.warning("Clarify pass crashed, proceeding with original prompt: %s", e, exc_info=True)
		return enhanced_prompt, []


async def _rescue_regenerate_changeset(
	original_prompt: str,
	failed_output: str,
	site_config: dict,
	event_callback,
	user_prompt: str = "",
	drift_reason: str | None = None,
) -> list[dict]:
	"""Last-resort rescue: regenerate the changeset from scratch when the crew drifted.

	Local coder models (qwen2.5-coder, llama3.1) sometimes pivot into explanatory
	mode after calling get_doctype_schema - they describe what they found instead
	of emitting a changeset. When that happens, we run ONE direct LLM call with
	the original user prompt + the failed attempt, asking for a clean JSON
	changeset in one shot. No tools, no agent loop - a single focused call.

	Args:
		original_prompt: The enhanced prompt fed to the crew. Used as the
			primary source of truth for what the user wants.
		failed_output: The developer agent's raw Final Answer (the drift).
			INTENTIONALLY OMITTED from the rescue LLM prompt when
			`drift_reason` is set - the drifted prose is noise and would
			just re-anchor the rescue LLM on the wrong content.
		site_config: LLM config (model, api_key, base_url, num_ctx).
		event_callback: For streaming "Rescue" events to the UI.
		user_prompt: The user's ORIGINAL raw prompt (pre-enhancement). Used
			to inject a hard DocType constraint into the rescue system
			message. This is the most reliable signal for "what the user
			actually asked about".
		drift_reason: If the caller already detected drift (see
			`alfred.api.pipeline._detect_drift`), pass the reason here.
			When set:
			  - The failed_output is NOT included in the rescue LLM prompt
			    (it would just re-anchor on the drift).
			  - A specific "your upstream drifted, do NOT copy it" note is
			    prepended to the rescue system message.

	Returns a normalized changeset list (possibly empty) - never raises.
	"""
	if not original_prompt:
		return []

	try:
		from alfred.llm_client import ollama_chat

		# Drift-aware preamble. When the upstream crew produced drift, we
		# tell the rescue LLM explicitly that a previous attempt went
		# off-topic, and we refuse to pass the failed output through.
		drift_preamble = ""
		if drift_reason:
			drift_preamble = (
				"URGENT - DRIFT RECOVERY MODE:\n"
				f"A previous attempt drifted off-topic ({drift_reason}). That "
				"drifted output is NOT shown to you here because it would just "
				"re-anchor your answer. Read ONLY the USER REQUEST below and "
				"generate a fresh changeset that targets the DocType named in "
				"the user's request. Do not reference Sales Order, Expense "
				"Claim, or any other DocType the user did not explicitly name.\n\n"
			)

		raw_user_ref = (user_prompt or original_prompt).strip()

		system = (
			drift_preamble
			+ "You are a Frappe changeset generator. Given a user request, produce a "
			"deployable JSON changeset that can be applied via frappe.get_doc(data).insert().\n\n"
			"OUTPUT FORMAT (STRICT):\n"
			"- Output ONLY a raw JSON array. No prose, no markdown, no code fences, no commentary.\n"
			"- Start with `[` and end with `]`.\n"
			"- Each item: {\"op\": \"create\", \"doctype\": \"<type>\", \"data\": {...complete document...}}\n"
			"- Every `data` object MUST include \"doctype\" matching the outer doctype plus all "
			"mandatory fields for that document type.\n\n"
			"STAY IN THE USER'S DOMAIN - CRITICAL:\n"
			"- The user's ORIGINAL raw request is pinned at the top of the user message below "
			"under 'USER RAW REQUEST'. The target DocType is whichever one the user named THERE.\n"
			"- If the user said 'Employee', emit items targeting Employee. If the user said "
			"'Leave Application', emit items targeting Leave Application. Do NOT substitute.\n"
			"- Read the USER RAW REQUEST TWICE before deciding on the target DocType.\n\n"
			"FRAPPE CUSTOMIZATION BASICS:\n"
			"- Email alert -> use Notification doctype (not Server Script)\n"
			"- New field on existing doctype -> Custom Field (not a new DocType)\n"
			"- Validation rule / business rule / custom constraint / 'throw a message' ->\n"
			"  Server Script (script_type='DocType Event', doctype_event='before_save' or\n"
			"  'before_insert' or 'validate'). The script body uses `doc.<field>` and calls\n"
			"  `frappe.throw('<user-facing message>')` to reject the save.\n"
			"  A validation is NEVER a Notification and NEVER a new DocType.\n"
			"- Required fields for Notification: name, subject, document_type, event, channel, "
			"recipients, message, enabled\n"
			"- Required fields for Server Script: name, script_type, reference_doctype, "
			"doctype_event, script\n"
			"- Required fields for Custom Field: name, dt, fieldname, fieldtype, label\n\n"
			"SHAPE OF EACH ITEM (placeholders in <angle brackets>):\n"
			'[{"op":"create","doctype":"<DocType kind>","data":{"doctype":"<DocType kind>","name":"<descriptive name>","...mandatory fields..."}}]\n\n'
			"If the request genuinely cannot be satisfied with any Frappe customization, "
			"output `[]`."
		)

		user_msg_parts = [
			f"USER RAW REQUEST (this is the authoritative source of target DocType and intent):\n{raw_user_ref[:2000]}",
		]
		if original_prompt and original_prompt != raw_user_ref:
			user_msg_parts.append(
				f"\n\nENHANCED SPEC (rewritten but must not contradict the raw request above):\n{original_prompt[:2000]}"
			)
		# Only include the failed output if drift was NOT detected. If
		# drift was detected, the prose is actively harmful - it would
		# just re-anchor the rescue LLM on the wrong content.
		if failed_output and not drift_reason:
			user_msg_parts.append(
				"\n\nThe agent pipeline produced this output, which is not a valid changeset - "
				"you may use it as hints but IGNORE its format:\n" + failed_output[:3000]
			)
		user_msg_parts.append("\n\nProduce the JSON changeset now. Raw JSON only.")

		await event_callback("reformat", {
			"agent": "Rescue", "status": "running",
			"message": "Crew drifted off-spec. Regenerating changeset from the original request...",
		})

		regenerated = await ollama_chat(
			messages=[
				{"role": "system", "content": system},
				{"role": "user", "content": "".join(user_msg_parts)},
			],
			site_config=site_config,
			tier="reasoning",
			max_tokens=2048,
			temperature=0.0,
			num_ctx_override=8192,
			timeout=int(site_config.get("llm_timeout") or 90),
		)
		logger.info(
			"Rescue regeneration result (first 500): %r", (regenerated or "")[:500],
		)
		changes = _extract_changes(regenerated)
		if changes:
			await event_callback("reformat", {
				"agent": "Rescue", "status": "success",
				"message": f"Rescue produced {len(changes)} item(s).",
			})
		else:
			await event_callback("reformat", {
				"agent": "Rescue", "status": "empty",
				"message": "Rescue pass produced no changeset - request may be out of scope.",
			})
		return changes
	except Exception as e:  # noqa: BLE001 — pre-existing master broad catch (best-effort path; revisit in TD-H3 follow-up)
		logger.warning("Rescue regeneration failed: %s", e, exc_info=True)
		return []


