"""Mode-specific lightweight handlers.

These bypass the full agent crew for modes that only need a single LLM call:
  - chat: pure conversational reply, no tools
  - insights: read-only Q&A with MCP tools (Phase B)

The Dev and Plan modes continue to go through the CrewAI pipeline in
`alfred.agents.crew`.
"""
