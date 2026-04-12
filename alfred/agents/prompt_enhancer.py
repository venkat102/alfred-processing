"""Prompt enhancer - rewrites a raw user prompt into a structured, Frappe-aware specification.

Runs a single LLM call before the agent pipeline to:
1. Clarify vague requests ("make a book thing" → "Create a DocType called Book...")
2. Identify the type of customization needed (DocType, Workflow, Server Script, etc.)
3. Infer reasonable defaults for fields, permissions, and naming
4. Add Frappe-specific context the agents need

This keeps the agent pipeline focused on execution rather than interpretation.
"""

import asyncio
import logging
import os

import litellm

from alfred.agents.frappe_knowledge import FRAPPE_REFERENCE

logger = logging.getLogger("alfred.prompt_enhancer")

_SYSTEM_PROMPT_PREFIX = """\
You are a Frappe/ERPNext expert. Your job is to take a user's raw request and rewrite it \
into a clear, detailed specification that a team of AI agents can execute.

CRITICAL RULES - follow these strictly:

1. IDENTIFY EXISTING FUNCTIONALITY FIRST.
   Frappe and ERPNext (including HRMS, Education, Healthcare, etc.) already have hundreds of \
   DocTypes, fields, workflows, and notifications built in. Before suggesting ANY new creation, \
   you MUST check whether the functionality already exists.

   Common existing DocTypes and their key fields:
   - Expense Claim: expense_approver, approval_status, total_claimed_amount, employee
   - Leave Application: leave_approver, status, employee, leave_type
   - Sales Order / Sales Invoice / Purchase Order / Purchase Invoice: standard accounting flow
   - Employee: employee_name, department, designation, reports_to
   - Customer / Supplier / Item / BOM / Stock Entry: standard ERPNext modules
   - Notification: built-in DocType for email/SMS alerts on document events

2. PREFER MINIMAL CHANGES.
   - If the user wants an email notification → use the built-in Notification DocType, NOT a Server Script
   - If the user wants a field added → use Custom Field on the existing DocType, do NOT create a new DocType
   - If the user wants a workflow → check if one already exists on that DocType first
   - If the user wants a report → check if a standard report already covers it
   - Only create new DocTypes when the user genuinely needs a new entity that doesn't exist

3. BE SPECIFIC about what already exists vs. what needs to be created.
   Example: "Expense Claim already exists in HRMS with an expense_approver field. \
   The task is ONLY to create a Notification that emails the expense_approver when a new \
   Expense Claim is submitted. No new DocType, Custom Field, or Server Script is needed."

4. Mention the exact Frappe customization type(s) needed:
   DocType, Custom Field, Server Script, Client Script, Workflow, Notification, Report, Print Format

5. If the request is ambiguous, make reasonable assumptions and state them.

Use the following Frappe reference to identify existing DocTypes, fields, and the right customization approach:

"""

SYSTEM_PROMPT = _SYSTEM_PROMPT_PREFIX + FRAPPE_REFERENCE + """

Output ONLY the enhanced prompt text. No JSON, no markdown headers, no code fences.
"""


async def enhance_prompt(
    raw_prompt: str,
    user_context: dict,
    site_config: dict,
) -> str:
    """Enhance a raw user prompt into a structured specification.

    Args:
        raw_prompt: The user's original message.
        user_context: Dict with user, roles, site_id.
        site_config: LLM configuration from Alfred Settings.

    Returns:
        Enhanced prompt string. Falls back to original prompt on any error.
    """
    model = site_config.get("llm_model") or os.environ.get("FALLBACK_LLM_MODEL") or "ollama/llama3.1"
    api_key = site_config.get("llm_api_key") or os.environ.get("FALLBACK_LLM_API_KEY") or ""
    base_url = site_config.get("llm_base_url") or os.environ.get("FALLBACK_LLM_BASE_URL") or ""

    # Only send relevant roles (not all 45+) to save tokens
    all_roles = user_context.get("roles", [])
    noise_roles = {"All", "Guest", "Desk User", "Newsletter Manager", "Translator",
                   "Prepared Report User", "Inbox User", "Knowledge Base Editor",
                   "Knowledge Base Contributor", "Dashboard Manager", "Workspace Manager",
                   "Report Manager", "Website Manager"}
    relevant_roles = [r for r in all_roles if r not in noise_roles]

    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": (
                f"User: {user_context.get('user', 'unknown')} "
                f"(roles: {', '.join(relevant_roles[:15])})\n\n"
                f"Request: {raw_prompt}"
            )},
        ],
        "max_tokens": 512,
        "temperature": 0.1,
        "stream": True,
        "timeout": 120,
    }
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
        kwargs["api_base"] = base_url

    # Ollama context window limit
    num_ctx = int(site_config.get("llm_num_ctx") or 0)
    if num_ctx > 0:
        kwargs["num_ctx"] = num_ctx
    elif model.startswith("ollama/"):
        kwargs["num_ctx"] = 8192  # Needs room for Frappe reference (~3k tokens) + prompt + response

    try:
        logger.info("Enhancing prompt: model=%s, original_length=%d", model, len(raw_prompt))
        loop = asyncio.get_event_loop()

        def _run():
            chunks = []
            for chunk in litellm.completion(**kwargs):
                token = chunk.choices[0].delta.content
                if token:
                    chunks.append(token)
            return "".join(chunks).strip()

        enhanced = await loop.run_in_executor(None, _run)
        logger.info("Prompt enhanced: original=%d chars, enhanced=%d chars", len(raw_prompt), len(enhanced))
        return enhanced if enhanced else raw_prompt
    except Exception as e:
        logger.warning("Prompt enhancement failed, using original: %s", e)
        return raw_prompt
