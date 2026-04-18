"""Chat mode handler - pure conversational reply, no crew, no tools.

This is the cheapest path through Alfred: one LLM call with conversation
memory as context, a small conversational system prompt, and no tool
bindings at all.

Use for:
  - Greetings, thanks, acknowledgements
  - Meta questions about Alfred itself ("what can you do?")
  - Recaps ("summarize what we built so far")
  - Anything that doesn't require site data or a changeset

NOT for:
  - Questions about the user's site state -> insights mode handler
  - Build/modify requests -> dev mode crew
  - Plan-the-approach requests -> plan mode crew

The handler returns a plain string. The pipeline phase that calls it is
responsible for emitting the `chat_reply` message to the WebSocket.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
	from alfred.state.conversation_memory import ConversationMemory

logger = logging.getLogger("alfred.handlers.chat")

_SYSTEM_PROMPT = """\
You are Alfred, an AI assistant that helps users customize their Frappe/ERPNext \
site by building DocTypes, workflows, notifications, server scripts, and other \
customizations through conversation.

You're in CHAT mode right now - the user sent a conversational message (a \
greeting, a thank-you, a meta question about you, or a recap question) rather \
than a build request. Reply briefly and warmly. If helpful, suggest what the \
user could try next: ask about their site (e.g., "what DocTypes do I have?"), \
ask for a plan (e.g., "how would we add approval to Expense Claims?"), or ask \
you to build something directly (e.g., "add a priority field to ToDo"). The user's actual request should decide the target DocType - these are only conversation starters.

Rules:
- Keep replies to 1-3 sentences unless the user is asking for a recap of what \
  was built in this conversation.
- Do NOT produce JSON, changesets, or code - this mode never deploys anything.
- Do NOT invent facts about the user's site. If the user asks about their DocTypes \
  or existing customizations, suggest they rephrase to get an Insights-mode response \
  (e.g., "what DocTypes do I have on this site?").
- If the user thanks you or says goodbye, respond in kind and offer to help with \
  anything else.
- Use plain text only. No markdown headers, no code fences, no bullet lists \
  unless the user explicitly asks for a list.
"""


async def handle_chat(
	prompt: str,
	memory: "ConversationMemory | None",
	user_context: dict,
	site_config: dict,
) -> str:
	"""Generate a conversational reply.

	Args:
		prompt: The user's raw message.
		memory: Optional conversation memory, rendered into the system prompt
			for context on earlier turns.
		user_context: Dict with user, roles, site_id (for personalization).
		site_config: LLM configuration from Alfred Settings.

	Returns:
		A plain-text reply string. Falls back to a generic acknowledgement
		on any LLM error - never raises.
	"""
	from alfred.llm_client import ollama_chat

	memory_context = ""
	if memory is not None:
		try:
			memory_context = memory.render_for_prompt()
		except Exception as e:
			logger.warning("memory.render_for_prompt failed in chat handler: %s", e)

	system_parts = [_SYSTEM_PROMPT]
	if memory_context:
		system_parts.append("")
		system_parts.append(memory_context)

	try:
		reply = await ollama_chat(
			messages=[
				{"role": "system", "content": "\n".join(system_parts)},
				{"role": "user", "content": prompt},
			],
			site_config=site_config,
			tier="triage",
			max_tokens=256,
			temperature=0.3,
			num_ctx_override=2048,
			timeout=int(site_config.get("llm_timeout") or 60),
		)
		if reply and reply.strip():
			return reply.strip()
	except Exception as e:
		logger.warning("Chat handler LLM call failed: %s", e)

	return (
		"Hi! I'm Alfred, your Frappe customization assistant. "
		"Let me know what you'd like to build, or ask about what's already "
		"on your site."
	)
