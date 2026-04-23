"""Lightweight LLM client for direct Ollama API calls.

litellm + httpx use httpcore which has a read-timeout bug when called from
a thread pool executor inside an asyncio event loop (the anyio backend hangs
on socket reads to remote Ollama). This module uses urllib (stdlib) instead,
which works reliably in all contexts.

All network + JSON parsing errors are raised as OllamaError so callers can
catch a single exception type without depending on urllib/http internals.

Used by: orchestrator classifier, prompt enhancer, chat handler, reflection.
NOT used by: CrewAI agent calls (CrewAI manages its own litellm internally).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger("alfred.llm_client")

# Model tier constants - used by callers to tag their calls.
TIER_TRIAGE = "triage"
TIER_REASONING = "reasoning"
TIER_AGENT = "agent"


# TD-H6 Phase 1: dedicated executor for LLM calls. Lazy-initialised on
# first use so the pool size can be read from Settings (which needs the
# FastAPI lifespan to set API_SECRET_KEY etc. in test environments).
# Threads are named `alfred-llm-*` so ps/jstack output distinguishes
# LLM-bound threads from FastAPI's default workers.
_llm_executor: concurrent.futures.ThreadPoolExecutor | None = None
_llm_executor_lock = threading.Lock()


def _get_llm_executor() -> concurrent.futures.ThreadPoolExecutor:
	"""Return the singleton LLM executor, initialising on first call.

	Read LLM_POOL_SIZE from Settings; fall back to 16 if Settings
	refuses to load (e.g., standalone script use without .env).
	"""
	global _llm_executor
	if _llm_executor is not None:
		return _llm_executor
	with _llm_executor_lock:
		if _llm_executor is not None:  # double-check inside the lock
			return _llm_executor
		try:
			from alfred.config import get_settings
			size = get_settings().LLM_POOL_SIZE
		except Exception:  # noqa: BLE001
			# Settings load failed - use a safe default so offline
			# utility scripts / early imports don't break.
			size = 16
		_llm_executor = concurrent.futures.ThreadPoolExecutor(
			max_workers=size,
			thread_name_prefix="alfred-llm",
		)
		logger.info("LLM executor initialised with %d workers", size)
		return _llm_executor


class OllamaError(RuntimeError):
	"""Raised for any network or protocol failure from the Ollama client."""


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

    # SSRF gate: client-supplied site_config.llm_base_url passes through
    # here verbatim; validate before any network I/O. Block scheme / private-
    # IP misuse so a compromised client can't point Alfred at cloud metadata
    # or internal services. See alfred/security/url_allowlist.py for policy.
    from alfred.security.url_allowlist import SsrfPolicyError, validate_llm_url
    try:
        validate_llm_url(base_url)
    except SsrfPolicyError as e:
        raise OllamaError(
            f"LLM URL rejected by SSRF policy ({e.reason}): {e}"
        ) from e

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
    def _record_error(error_type: str) -> None:
        # Prometheus counter for dashboards + alerting. Import inside the
        # helper so a broken metrics import never shadows the real LLM error.
        try:
            from alfred.obs.metrics import llm_errors_total
            llm_errors_total.labels(
                tier=tier or "default", error_type=error_type,
            ).inc()
        except Exception:  # noqa: BLE001
            pass

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        _record_error("http_error")
        raise OllamaError(
            f"Ollama HTTP {e.code} from {url} (model={ollama_model}): {body}"
        ) from e
    except urllib.error.URLError as e:
        _record_error("network_error")
        raise OllamaError(
            f"Ollama network error from {url} (model={ollama_model}): {e.reason}"
        ) from e
    except TimeoutError as e:
        _record_error("timeout")
        raise OllamaError(
            f"Ollama timeout after {timeout}s from {url} (model={ollama_model})"
        ) from e

    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        _record_error("non_json")
        snippet = raw[:200] if isinstance(raw, (bytes, bytearray)) else str(raw)[:200]
        raise OllamaError(
            f"Ollama returned non-JSON response from {url} (model={ollama_model}): {snippet!r}"
        ) from e

    if not isinstance(data, dict):
        _record_error("unexpected_payload")
        raise OllamaError(
            f"Ollama returned unexpected payload type {type(data).__name__} "
            f"(expected object) from {url} (model={ollama_model})"
        )
    message = data.get("message")
    if not isinstance(message, dict):
        _record_error("missing_message")
        raise OllamaError(
            f"Ollama response missing 'message' object from {url} (model={ollama_model}): {data!r}"
        )
    return (message.get("content") or "").strip()


async def ollama_chat(
    messages: list[dict[str, str]],
    site_config: dict,
    *,
    tier: str | None = None,
    **kwargs,
) -> str:
    """Async wrapper - runs ollama_chat_sync in the dedicated LLM pool.

    TD-H6 Phase 1: uses its own ThreadPoolExecutor (see
    ``_get_llm_executor``) instead of FastAPI's default. A spike of
    concurrent LLM calls can't starve the rest of the event loop's
    blocking work (heartbeat, health checks, admin calls).
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _get_llm_executor(),
        lambda: ollama_chat_sync(messages, site_config, tier=tier, **kwargs),
    )
