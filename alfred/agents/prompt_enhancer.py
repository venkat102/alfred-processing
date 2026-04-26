"""Prompt enhancer - rewrites a raw user prompt into a structured, Frappe-aware specification.

Runs a single LLM call before the agent pipeline to:
1. Clarify vague requests ("make a book thing" → "Create a DocType called Book...")
2. Identify the type of customization needed (DocType, Workflow, Server Script, etc.)
3. Infer reasonable defaults for fields, permissions, and naming
4. Add Frappe-specific context the agents need

This keeps the agent pipeline focused on execution rather than interpretation.
"""

import logging

from alfred.agents.frappe_knowledge import FRAPPE_REFERENCE

logger = logging.getLogger("alfred.prompt_enhancer")

_SYSTEM_PROMPT_PREFIX = """\
You are a Frappe/ERPNext expert. Your job is to take a user's raw request and rewrite it \
into a clear, detailed specification that a team of AI agents can execute.

CRITICAL RULES - follow these strictly:

1. IDENTIFY EXISTING FUNCTIONALITY FIRST.
   Frappe and ERPNext (including HRMS, Education, Healthcare, etc.) already have hundreds of \
   built-in DocTypes, fields, workflows, and notifications. Before suggesting ANY new creation, \
   you MUST check whether the functionality already exists. Do NOT assume a DocType exists or \
   has a particular field - downstream agents verify everything against the live site via \
   `get_doctype_schema`. Your job is to keep the enhanced prompt faithful to what the user \
   actually asked for.

2. STAY IN THE USER'S DOMAIN.
   Do NOT invent examples, DocTypes, or fields that the user did not mention. If the user \
   asks about "orders", rewrite for orders - do not pivot to leave applications or any other \
   domain because it is easier to describe. Use placeholder phrasing like "<target DocType>" \
   or "<link field holding the approver>" if you need to describe structure without \
   committing to field names that haven't been verified.

3. MENTION THE EXACT FRAPPE CUSTOMIZATION TYPE(S) NEEDED:
   DocType, Custom Field, Server Script, Client Script, Workflow, Notification, Report, Print Format.
   Do NOT prescribe implementation details (specific events, trigger conditions, field types,
   Python body text) - downstream agents have the Frappe Knowledge Base auto-injected into
   their task turn with the relevant platform rules, APIs, idioms, and house style. Your
   job is to name the target primitive and the target DocType cleanly; platform details
   land via the KB auto-inject phase.

4. IF THE REQUEST IS AMBIGUOUS, state the ambiguity clearly at the end of the enhanced prompt \
   as a question the downstream clarification gate can ask the user. Do NOT silently assume.

Use the following Frappe reference to identify existing DocTypes, fields, and the right \
customization approach - but remember the reference is a starting point, not authoritative \
truth for the live site. Downstream agents will query `lookup_doctype` and `lookup_pattern` \
to verify facts:

"""

SYSTEM_PROMPT = _SYSTEM_PROMPT_PREFIX + FRAPPE_REFERENCE + """

Output ONLY the enhanced prompt text. No JSON, no markdown headers, no code fences.
"""


async def enhance_prompt(
    raw_prompt: str,
    user_context: dict,
    site_config: dict,
    conversation_context: str | None = None,
) -> str:
    """Enhance a raw user prompt into a structured specification.

    Args:
        raw_prompt: The user's original message.
        user_context: Dict with user, roles, site_id.
        site_config: LLM configuration from Alfred Settings.
        conversation_context: Optional block summarizing what earlier turns in
            this chat already built or clarified. Prepended to the user message
            so the enhancer can resolve references like "that DocType" to a
            concrete name from history.

    Returns:
        Enhanced prompt string. Falls back to original prompt on any error.
    """
    from alfred.llm_client import ollama_chat

    # Only send relevant roles (not all 45+) to save tokens
    all_roles = user_context.get("roles", [])
    noise_roles = {"All", "Guest", "Desk User", "Newsletter Manager", "Translator",
                   "Prepared Report User", "Inbox User", "Knowledge Base Editor",
                   "Knowledge Base Contributor", "Dashboard Manager", "Workspace Manager",
                   "Report Manager", "Website Manager"}
    relevant_roles = [r for r in all_roles if r not in noise_roles]

    user_message_parts = [
        f"User: {user_context.get('user', 'unknown')} "
        f"(roles: {', '.join(relevant_roles[:15])})",
    ]
    if conversation_context:
        user_message_parts.append("")
        user_message_parts.append(conversation_context)
    user_message_parts.append("")
    user_message_parts.append(f"Request: {raw_prompt}")

    try:
        logger.info("Enhancing prompt: original_length=%d", len(raw_prompt))

        enhanced = await ollama_chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(user_message_parts)},
            ],
            site_config=site_config,
            tier="reasoning",
            max_tokens=512,
            temperature=0.1,
            # Needs room for Frappe reference (~3k tokens) + prompt + response
            num_ctx_override=8192,
            timeout=int(site_config.get("llm_timeout") or 120),
        )
        logger.info("Prompt enhanced: original=%d chars, enhanced=%d chars", len(raw_prompt), len(enhanced))
        return enhanced if enhanced else raw_prompt
    except Exception as e:  # noqa: BLE001 — LLM-boundary contract; any backend failure (OllamaError, timeout, runtime error from mocks) degrades to the original prompt rather than crash the pipeline
        logger.warning("Prompt enhancement failed, using original: %s", e)
        return raw_prompt
