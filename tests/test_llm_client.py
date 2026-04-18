"""Tests for the urllib-based Ollama LLM client.

Covers:
  - Tier resolution: default model, per-tier override, fallback when
    tier field is empty, env-var fallback.
  - ollama_chat_sync happy path: request assembly, response parsing.
  - Error wrapping: HTTPError, URLError, TimeoutError, malformed JSON,
    non-dict payload, missing "message" field all raise OllamaError.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from alfred import llm_client
from alfred.llm_client import (
	OllamaError,
	_resolve_ollama_config,
	_resolve_ollama_config_for_tier,
	ollama_chat,
	ollama_chat_sync,
)


def _run(coro):
	return asyncio.new_event_loop().run_until_complete(coro)


class _FakeResponse:
	"""Context-manager stand-in for urllib.request.urlopen's return value."""

	def __init__(self, body: bytes):
		self._body = body

	def __enter__(self):
		return self

	def __exit__(self, *args):
		return False

	def read(self) -> bytes:
		return self._body


class TestResolveOllamaConfig:
	def test_default_values_when_site_config_empty(self, monkeypatch):
		monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
		monkeypatch.delenv("FALLBACK_LLM_BASE_URL", raising=False)
		model, url, ctx = _resolve_ollama_config({})
		assert model == "ollama/llama3.1"
		assert url == "http://localhost:11434"
		assert ctx == 0

	def test_site_config_overrides_default(self, monkeypatch):
		monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
		cfg = {
			"llm_model": "ollama/qwen2.5-coder:32b",
			"llm_base_url": "http://10.0.0.1:11434",
			"llm_num_ctx": "8192",
		}
		model, url, ctx = _resolve_ollama_config(cfg)
		assert model == "ollama/qwen2.5-coder:32b"
		assert url == "http://10.0.0.1:11434"
		assert ctx == 8192

	def test_env_vars_bridge_missing_fields(self, monkeypatch):
		monkeypatch.setenv("FALLBACK_LLM_MODEL", "ollama/env-model")
		monkeypatch.setenv("FALLBACK_LLM_BASE_URL", "http://env-host:11434")
		model, url, _ = _resolve_ollama_config({})
		assert model == "ollama/env-model"
		assert url == "http://env-host:11434"


class TestResolveOllamaConfigForTier:
	def test_tier_none_falls_back_to_default_model(self, monkeypatch):
		monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)
		cfg = {"llm_model": "ollama/default", "llm_num_ctx": 4096}
		model, _, ctx = _resolve_ollama_config_for_tier(cfg, tier=None)
		assert model == "ollama/default"
		assert ctx == 4096

	def test_tier_with_override_uses_tier_model(self):
		cfg = {
			"llm_model": "ollama/default",
			"llm_model_triage": "ollama/gemma:2b",
			"llm_model_triage_num_ctx": 2048,
		}
		model, _, ctx = _resolve_ollama_config_for_tier(cfg, tier="triage")
		assert model == "ollama/gemma:2b"
		assert ctx == 2048

	def test_empty_tier_field_falls_back_to_default(self):
		cfg = {
			"llm_model": "ollama/default",
			"llm_num_ctx": 4096,
			"llm_model_triage": "",
			"llm_model_triage_num_ctx": 0,
		}
		model, _, ctx = _resolve_ollama_config_for_tier(cfg, tier="triage")
		assert model == "ollama/default"
		assert ctx == 4096

	def test_whitespace_tier_field_falls_back(self):
		cfg = {
			"llm_model": "ollama/default",
			"llm_model_agent": "   ",
		}
		model, _, _ = _resolve_ollama_config_for_tier(cfg, tier="agent")
		assert model == "ollama/default"

	def test_base_url_is_shared_across_tiers(self):
		cfg = {
			"llm_base_url": "http://shared:11434",
			"llm_model_reasoning": "ollama/reasoner",
		}
		_, url, _ = _resolve_ollama_config_for_tier(cfg, tier="reasoning")
		assert url == "http://shared:11434"


class TestOllamaChatSyncHappyPath:
	def test_builds_and_parses_response(self):
		cfg = {
			"llm_model": "ollama/test-model",
			"llm_base_url": "http://localhost:11434",
		}
		body = json.dumps({"message": {"content": "hello world"}}).encode()
		captured = {}

		def fake_urlopen(req, timeout=None):
			captured["url"] = req.full_url
			captured["body"] = json.loads(req.data)
			captured["timeout"] = timeout
			return _FakeResponse(body)

		with patch("alfred.llm_client.urllib.request.urlopen", side_effect=fake_urlopen):
			result = ollama_chat_sync(
				[{"role": "user", "content": "hi"}],
				cfg,
				max_tokens=64,
				temperature=0.2,
				num_ctx_override=1024,
				timeout=10,
			)

		assert result == "hello world"
		assert captured["url"] == "http://localhost:11434/api/chat"
		assert captured["timeout"] == 10
		assert captured["body"]["model"] == "test-model"
		assert captured["body"]["stream"] is False
		assert captured["body"]["options"]["num_predict"] == 64
		assert captured["body"]["options"]["temperature"] == 0.2
		assert captured["body"]["options"]["num_ctx"] == 1024

	def test_tier_model_is_used_when_set(self):
		cfg = {
			"llm_model": "ollama/default",
			"llm_model_triage": "ollama/gemma:2b",
			"llm_base_url": "http://localhost:11434",
		}
		body = json.dumps({"message": {"content": "ok"}}).encode()
		captured = {}

		def fake_urlopen(req, timeout=None):
			captured["body"] = json.loads(req.data)
			return _FakeResponse(body)

		with patch("alfred.llm_client.urllib.request.urlopen", side_effect=fake_urlopen):
			ollama_chat_sync([{"role": "user", "content": "?"}], cfg, tier="triage")
		assert captured["body"]["model"] == "gemma:2b"


class TestOllamaChatSyncErrorHandling:
	_cfg = {
		"llm_model": "ollama/test-model",
		"llm_base_url": "http://localhost:11434",
	}

	def test_http_error_is_wrapped(self):
		err = urllib.error.HTTPError(
			"http://localhost:11434/api/chat",
			500,
			"Server Error",
			{},
			io.BytesIO(b"upstream explosion"),
		)
		with patch("alfred.llm_client.urllib.request.urlopen", side_effect=err):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert "HTTP 500" in str(exc_info.value)
		assert "test-model" in str(exc_info.value)

	def test_url_error_is_wrapped(self):
		err = urllib.error.URLError("connection refused")
		with patch("alfred.llm_client.urllib.request.urlopen", side_effect=err):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert "network error" in str(exc_info.value)

	def test_timeout_error_is_wrapped(self):
		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			side_effect=TimeoutError("read timed out"),
		):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg, timeout=5)
		assert "timeout after 5s" in str(exc_info.value)

	def test_malformed_json_is_wrapped(self):
		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			return_value=_FakeResponse(b"not json at all"),
		):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert "non-JSON" in str(exc_info.value)

	def test_non_dict_payload_is_wrapped(self):
		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			return_value=_FakeResponse(b"[1, 2, 3]"),
		):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert "unexpected payload type" in str(exc_info.value)

	def test_missing_message_field_is_wrapped(self):
		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			return_value=_FakeResponse(b'{"error": "not found"}'),
		):
			with pytest.raises(OllamaError) as exc_info:
				ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert "missing 'message'" in str(exc_info.value)

	def test_empty_content_returns_empty_string(self):
		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			return_value=_FakeResponse(b'{"message": {"content": ""}}'),
		):
			result = ollama_chat_sync([{"role": "user", "content": "x"}], self._cfg)
		assert result == ""


class TestOllamaChatAsync:
	def test_async_wrapper_delegates_to_sync(self):
		cfg = {"llm_model": "ollama/test"}
		with patch("alfred.llm_client.ollama_chat_sync", return_value="delegated") as stub:
			result = _run(ollama_chat([{"role": "user", "content": "x"}], cfg, tier="triage"))
		assert result == "delegated"
		stub.assert_called_once()
		_, kwargs = stub.call_args
		assert kwargs["tier"] == "triage"
