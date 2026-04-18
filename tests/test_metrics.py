"""Tests for the Prometheus /metrics endpoint and the four counters.

We verify:
- /metrics returns text that prometheus_client can parse (exposition format)
- Every counter / histogram name we rely on shows up in the scrape output
- Counters increment when the code paths that own them are exercised

The counters are declared in alfred.obs.metrics and mutated inside:
- alfred.api.pipeline.AgentPipeline.run -> pipeline_phase_duration_seconds
- alfred.tools.mcp_tools._mcp_call -> mcp_calls_total
- alfred.orchestrator.classify_mode -> orchestrator_decisions_total
- alfred.llm_client.ollama_chat_sync -> llm_errors_total
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from alfred.llm_client import OllamaError
from alfred.main import create_app
from alfred.obs import metrics
from alfred.obs.metrics import (
	llm_errors_total,
	mcp_calls_total,
	orchestrator_decisions_total,
	pipeline_phase_duration_seconds,
)


@pytest.fixture
def app():
	import os
	os.environ["API_SECRET_KEY"] = "test-secret"
	return create_app()


@pytest.fixture(autouse=True)
def _reset_metrics():
	metrics.reset_for_tests()
	yield
	metrics.reset_for_tests()


async def _scrape(app) -> str:
	async with AsyncClient(
		transport=ASGITransport(app=app),
		base_url="http://test",
		follow_redirects=True,
	) as client:
		resp = await client.get("/metrics")
		assert resp.status_code == 200
		return resp.text


class TestMetricsEndpoint:
	async def test_metrics_endpoint_returns_prometheus_exposition(self, app):
		# Touch each metric so its family appears in the scrape.
		pipeline_phase_duration_seconds.labels(phase="sanitize").observe(0.001)
		mcp_calls_total.labels(tool="get_site_info", outcome="success").inc()
		orchestrator_decisions_total.labels(source="fast_path", mode="chat").inc()
		llm_errors_total.labels(tier="triage", error_type="timeout").inc()

		text = await _scrape(app)
		# Name presence is what matters. Exposition format pads them with
		# HELP / TYPE lines plus the labelled samples.
		for name in (
			"alfred_pipeline_phase_duration_seconds",
			"alfred_mcp_calls_total",
			"alfred_orchestrator_decisions_total",
			"alfred_llm_errors_total",
		):
			assert name in text, f"{name} not in /metrics output"


class TestMcpCounter:
	def test_mcp_call_increments_counter_on_success(self):
		from alfred.tools.mcp_tools import _mcp_call

		class FakeClient:
			run_state = None
			def call_sync(self, tool, args, timeout=None):
				return {"ok": True}

		_mcp_call(FakeClient(), "get_site_info", {})
		# Exposition-format sample for a labelled counter:
		#   alfred_mcp_calls_total{tool="get_site_info",outcome="success"} 1.0
		sample = next(
			s for m in mcp_calls_total.collect()
			for s in m.samples
			if s.labels.get("outcome") == "success"
		)
		assert sample.value == 1.0
		assert sample.labels["tool"] == "get_site_info"

	def test_mcp_call_increments_error_on_exception(self):
		from alfred.tools.mcp_tools import _mcp_call

		class BrokenClient:
			run_state = None
			def call_sync(self, tool, args, timeout=None):
				raise RuntimeError("explode")

		_mcp_call(BrokenClient(), "get_site_info", {})
		errors = [
			s for m in mcp_calls_total.collect() for s in m.samples
			if s.labels.get("outcome") == "error"
		]
		assert any(s.value == 1.0 for s in errors)


class TestOrchestratorCounter:
	def test_fast_path_match_increments_fast_path_label(self):
		from alfred.orchestrator import classify_mode

		async def _run():
			return await classify_mode(
				prompt="hi",
				memory=None,
				manual_override="auto",
				site_config={},
			)

		asyncio.new_event_loop().run_until_complete(_run())
		fast = [
			s for m in orchestrator_decisions_total.collect() for s in m.samples
			if s.labels.get("source") == "fast_path"
		]
		assert any(s.value == 1.0 for s in fast)

	def test_override_bypass_increments_override_label(self):
		from alfred.orchestrator import classify_mode

		async def _run():
			return await classify_mode(
				prompt="whatever",
				memory=None,
				manual_override="dev",
				site_config={},
			)

		asyncio.new_event_loop().run_until_complete(_run())
		override = [
			s for m in orchestrator_decisions_total.collect() for s in m.samples
			if s.labels.get("source") == "override"
		]
		assert any(s.value == 1.0 for s in override)


class TestLlmErrorCounter:
	def test_http_error_increments_counter(self):
		import urllib.error

		from alfred.llm_client import ollama_chat_sync
		import io
		from unittest.mock import patch

		err = urllib.error.HTTPError(
			"http://x/api/chat", 500, "Server Error", {}, io.BytesIO(b"boom"),
		)
		with patch("alfred.llm_client.urllib.request.urlopen", side_effect=err):
			with pytest.raises(OllamaError):
				ollama_chat_sync(
					[{"role": "user", "content": "x"}],
					{"llm_model": "ollama/test", "llm_base_url": "http://x"},
					tier="triage",
				)

		samples = [
			s for m in llm_errors_total.collect() for s in m.samples
			if s.labels.get("error_type") == "http_error"
		]
		assert any(s.value == 1.0 for s in samples)

	def test_timeout_error_increments_counter(self):
		from alfred.llm_client import ollama_chat_sync
		from unittest.mock import patch

		with patch(
			"alfred.llm_client.urllib.request.urlopen",
			side_effect=TimeoutError("read timed out"),
		):
			with pytest.raises(OllamaError):
				ollama_chat_sync(
					[{"role": "user", "content": "x"}],
					{"llm_model": "ollama/test", "llm_base_url": "http://x"},
					tier="reasoning",
				)

		samples = [
			s for m in llm_errors_total.collect() for s in m.samples
			if s.labels.get("error_type") == "timeout"
		]
		assert any(
			s.value == 1.0 and s.labels.get("tier") == "reasoning" for s in samples
		)
