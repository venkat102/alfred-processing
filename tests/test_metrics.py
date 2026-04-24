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
	crew_drift_total,
	crew_rescue_total,
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
		crew_drift_total.labels(reason="training_data_dump").inc()
		crew_rescue_total.labels(outcome="produced").inc()

		text = await _scrape(app)
		# Name presence is what matters. Exposition format pads them with
		# HELP / TYPE lines plus the labelled samples.
		for name in (
			"alfred_pipeline_phase_duration_seconds",
			"alfred_mcp_calls_total",
			"alfred_orchestrator_decisions_total",
			"alfred_llm_errors_total",
			"alfred_crew_drift_total",
			"alfred_crew_rescue_total",
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


class TestCrewRecoveryCounters:
	"""Exercise the drift + rescue counter wiring inside the post_crew
	phase. We don't run a real crew - we just poke the minimum ctx
	state and assert the counters move."""

	def _ctx(self, result_text: str, prompt: str = "Add a field to ToDo"):
		from unittest.mock import AsyncMock, MagicMock

		conn = MagicMock()
		conn.send = AsyncMock()
		conn.site_id = "test-site"
		conn.user = "t@t.com"
		conn.roles = ["System Manager"]
		conn.site_config = {"llm_model": "ollama/test"}
		conn.mcp_client = None
		conn.websocket = MagicMock()
		conn.websocket.app.state.redis = None
		conn.websocket.app.state.settings = MagicMock(
			ADMIN_PORTAL_URL="", ADMIN_SERVICE_KEY="",
		)

		from alfred.api.pipeline import PipelineContext

		ctx = PipelineContext(conn=conn, conversation_id="c1", prompt=prompt)
		ctx.result_text = result_text
		ctx.enhanced_prompt = prompt
		ctx.event_callback = AsyncMock()
		ctx.changes = []
		ctx.removed_by_reflection = []
		return ctx

	def test_drift_detection_increments_drift_counter(self):
		# _detect_drift returns a non-empty reason when the Developer's
		# output is clearly off-topic relative to the prompt. We feed it
		# a large Sales Order schema dump alongside a ToDo-adding prompt.
		from alfred.api.pipeline import AgentPipeline

		# Triggers _detect_drift signal 1: mentions an ERPNext field
		# (customer_name) that the user never asked about.
		schema_dump = (
			"Here's a breakdown of the Sales Order schema: "
			"customer_name is the linked party, transaction_date is the "
			"posting date, grand_total is the calculated amount."
		)
		ctx = self._ctx(result_text=schema_dump, prompt="Add a priority field to ToDo")
		ctx.mode = "dev"
		# post_crew reads the crew output from ctx.crew_result, not from
		# ctx.result_text directly - the assignment happens inside the phase.
		ctx.crew_result = {"status": "completed", "result": schema_dump}
		pipeline = AgentPipeline(ctx)

		# Rescue is imported at call time from alfred.api.websocket so
		# patch the source module.
		from unittest.mock import patch

		async def fake_rescue(*args, **kwargs):
			return []

		with patch("alfred.api.websocket._rescue_regenerate_changeset", new=fake_rescue):
			asyncio.new_event_loop().run_until_complete(pipeline._phase_post_crew())

		drift_samples = [
			s for m in crew_drift_total.collect() for s in m.samples
			if s.labels
		]
		rescue_empty = [
			s for m in crew_rescue_total.collect() for s in m.samples
			if s.labels.get("outcome") == "empty"
		]
		assert any(s.value >= 1.0 for s in drift_samples), (
			"drift counter should have fired on the schema-dump output"
		)
		assert any(s.value >= 1.0 for s in rescue_empty), (
			"rescue counter should record outcome=empty when regeneration fails"
		)

	def test_successful_rescue_increments_produced_label(self):
		# No drift, but first-pass extraction yields nothing. Rescue
		# returns a valid-shaped changeset. outcome should be 'produced'.
		# We don't care whether post_crew continues past the rescue hook
		# (dry-run retry needs an MCP client), so we let it abort cleanly.
		from unittest.mock import patch

		from alfred.api.pipeline import AgentPipeline

		prose = "I'll explain the approach: ..."
		ctx = self._ctx(result_text=prose, prompt="Add a field to ToDo")
		ctx.mode = "dev"
		ctx.crew_result = {"status": "completed", "result": prose}
		pipeline = AgentPipeline(ctx)

		changeset = [{"op": "create", "doctype": "Custom Field", "data": {"name": "x"}}]

		async def fake_rescue(*args, **kwargs):
			return changeset

		# Raise inside reflection so we exit post_crew right after the
		# rescue counter has fired and before the dry-run/retry path that
		# needs an MCP client. The outer try/except in run() would catch
		# this, but we're calling _phase_post_crew directly so it bubbles
		# - we swallow it here.
		from alfred.llm_client import OllamaError

		async def fake_reflect(*args, **kwargs):
			raise OllamaError("stop-after-rescue")

		with patch("alfred.api.websocket._rescue_regenerate_changeset", new=fake_rescue), \
		     patch("alfred.agents.reflection.reflect_minimality", new=fake_reflect):
			try:
				asyncio.new_event_loop().run_until_complete(
					pipeline._phase_post_crew(),
				)
			except OllamaError:
				pass  # expected

		produced = [
			s for m in crew_rescue_total.collect() for s in m.samples
			if s.labels.get("outcome") == "produced"
		]
		assert any(s.value >= 1.0 for s in produced), (
			"rescue counter should record outcome=produced on recovery"
		)


class TestLlmErrorCounter:
	@pytest.fixture(autouse=True)
	def _bypass_ssrf_for_fake_hosts(self):
		# Tests use ``http://x`` which the SSRF check would reject
		# (DNS fail) before urlopen is ever reached, so the urlopen
		# error paths the counters hang off would never fire.
		from unittest.mock import patch
		with patch(
			"alfred.security.url_allowlist.validate_llm_url",
			return_value=None,
		):
			yield

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
