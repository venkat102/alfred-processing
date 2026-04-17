"""Lightweight LLM client for direct Ollama API calls.

litellm + httpx use httpcore which has a read-timeout bug when called from
a thread pool executor inside an asyncio event loop (the anyio backend hangs
on socket reads to remote Ollama). This module uses urllib (stdlib) instead,
which works reliably in all contexts.

Used by: orchestrator classifier, prompt enhancer, chat handler, reflection.
NOT used by: CrewAI agent calls (CrewAI manages its own litellm internally).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.request
from typing import Any

logger = logging.getLogger("alfred.llm_client")

# Model tier constants - used by callers to tag their calls.
TIER_TRIAGE = "triage"
TIER_REASONING = "reasoning"
TIER_AGENT = "agent"


def _resolve_ollama_config(site_config: dict) -> tuple[str, str, int]:
    """Extract (model, base_url, num_ctx) from site_config + env fallbacks."""
    model = (
        site_config.get("llm_model")
        or os.environ.get("FALLBACK_LLM_MODEL")
        or "ollama/llama3.1"
    )
    base_url = (
        site_config.get("llm_base_url")
        or os.environ.get("FALLBACK_LLM_BASE_URL")
        or "http://localhost:11434"
    )
    num_ctx = int(site_config.get("llm_num_ctx") or 0)
    return model, base_url, num_ctx


def _resolve_ollama_config_for_tier(
    site_config: dict,
    tier: str | None = None,
) -> tuple[str, str, int]:
    """Extract (model, base_url, num_ctx) for a specific tier.

    Checks site_config["llm_model_{tier}"] first; falls back to the
    default model if the tier-specific field is empty.
    base_url is always shared (one Ollama server).
    """
    # base_url is shared across all tiers
    base_url = (
        site_config.get("llm_base_url")
        or os.environ.get("FALLBACK_LLM_BASE_URL")
        or "http://localhost:11434"
    )

    # Try tier-specific model
    model = ""
    num_ctx = 0
    if tier:
        model = (site_config.get(f"llm_model_{tier}") or "").strip()
        num_ctx = int(site_config.get(f"llm_model_{tier}_num_ctx") or 0)

    # Fallback to default model
    if not model:
        model = (
            site_config.get("llm_model")
            or os.environ.get("FALLBACK_LLM_MODEL")
            or "ollama/llama3.1"
        )
        num_ctx = int(site_config.get("llm_num_ctx") or 0)

    return model, base_url, num_ctx


def ollama_chat_sync(
    messages: list[dict[str, str]],
    site_config: dict,
    *,
    max_tokens: int = 256,
    temperature: float = 0.1,
    num_ctx_override: int | None = None,
    timeout: int = 60,
    tier: str | None = None,
) -> str:
    """Synchronous Ollama /api/chat call via urllib.

    Args:
        messages: OpenAI-style message list [{"role": ..., "content": ...}].
        site_config: LLM config from Alfred Settings.
        max_tokens: Max tokens to generate.
        temperature: Sampling temperature.
        num_ctx_override: Override context window size (None = use site_config or default).
        timeout: HTTP timeout in seconds.
        tier: Model tier ("triage", "reasoning", "agent"). None = use default model.

    Returns:
        The assistant's response content as a string.

    Raises:
        Exception on HTTP or JSON errors (caller should catch).
    """
    model, base_url, num_ctx = _resolve_ollama_config_for_tier(site_config, tier)
    ollama_model = model.removeprefix("ollama/")

    if num_ctx_override is not None:
        num_ctx = num_ctx_override
    elif num_ctx <= 0 and model.startswith("ollama/"):
        num_ctx = 4096

    options: dict[str, Any] = {
        "temperature": temperature,
        "num_predict": max_tokens,
    }
    if num_ctx > 0:
        options["num_ctx"] = num_ctx

    payload = json.dumps({
        "model": ollama_model,
        "messages": messages,
        "stream": False,
        "options": options,
    }).encode()

    url = f"{base_url.rstrip('/')}/api/chat"
    logger.debug(
        "ollama_chat_sync: %s model=%s tier=%s timeout=%s",
        url, ollama_model, tier or "default", timeout,
    )

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())

    return (data.get("message", {}).get("content") or "").strip()


async def ollama_chat(
    messages: list[dict[str, str]],
    site_config: dict,
    *,
    tier: str | None = None,
    **kwargs,
) -> str:
    """Async wrapper - runs ollama_chat_sync in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, lambda: ollama_chat_sync(messages, site_config, tier=tier, **kwargs)
    )
