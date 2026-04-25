"""Pin the TokenTracker wiring into ``_phase_run_crew``.

Before the audit's M2 fix, the class lived in
``alfred/agents/token_tracker.py`` and was only ever instantiated by
``tests/test_phase6.py`` — production never emitted token telemetry.
The wire fans CrewAI's per-agent ``_token_process`` summaries into
the tracker after ``crew.kickoff`` finishes, then sends a ``usage``
WebSocket event so the UI / REST client can see cost per run.

Each test below focuses on one observable from the wire:

  - tracker is populated when the crew has agents that recorded tokens;
  - tracker stays None for non-dev modes so we don't ship empty usage;
  - the ``usage`` event lands on the connection's ``send`` channel
    with the per-agent breakdown CrewAI exposed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.api.pipeline._phases_build import _PhasesBuildMixin
from alfred.api.pipeline.context import PipelineContext


def _fake_agent(role: str, prompt_tokens: int, completion_tokens: int):
	"""CrewAI agent stand-in that just exposes ``_token_process``.

	Real CrewAI agents store token counts on a TokenProcess object
	whose ``get_summary()`` returns a UsageMetrics-shaped Pydantic
	model. SimpleNamespace satisfies the same attribute lookups
	(``prompt_tokens``, ``completion_tokens``) without dragging in the
	full LLM stack."""
	summary = SimpleNamespace(
		prompt_tokens=prompt_tokens,
		completion_tokens=completion_tokens,
		total_tokens=prompt_tokens + completion_tokens,
	)
	token_process = MagicMock()
	token_process.get_summary.return_value = summary
	return SimpleNamespace(role=role, _token_process=token_process)


def _make_ctx(*, mode: str, agents: list, send: AsyncMock) -> PipelineContext:
	"""Build a PipelineContext with just enough surface for the
	post-kickoff token-fanout block to run end-to-end."""
	conn = MagicMock()
	conn.send = send
	conn.site_id = "site-a"
	conn.site_config = {"task_timeout_seconds": 30, "llm_model": "ollama/llama3.1"}

	ctx = PipelineContext(conn=conn, conversation_id="conv-x", prompt="x")
	ctx.mode = mode
	ctx.crew = SimpleNamespace(agents=agents)
	# CrewState only needs to be non-None — the assert in _phase_run_crew
	# guards on it but the real value isn't read for the token-fanout work.
	ctx.crew_state = MagicMock()
	# crew_result is what _phase_run_crew normally writes from run_crew —
	# preset it so the test can stub run_crew out without changing globals.
	ctx.crew_result = {"status": "completed", "result": ""}
	return ctx


class _PipelineForTest(_PhasesBuildMixin):
	"""Tiny concrete class so we can call the mixin's async methods.

	The real AgentPipeline composes several mixins; this one only
	needs the build-phase mixin under test."""

	def __init__(self, ctx: PipelineContext):
		self.ctx = ctx


@pytest.mark.asyncio
async def test_run_crew_populates_token_tracker_with_per_agent_breakdown(
	monkeypatch,
):
	"""Healthy dev-mode run: every agent's ``_token_process`` summary is
	folded into ``ctx.token_tracker`` and the per-agent breakdown is
	visible in the resulting summary."""
	send = AsyncMock()
	ctx = _make_ctx(
		mode="dev",
		agents=[
			_fake_agent("Solution Architect", 100, 50),
			_fake_agent("Frappe Developer", 200, 80),
		],
		send=send,
	)

	# Stub run_crew so the test is independent of LLM / Ollama.
	async def _fake_run_crew(*_, **__):
		return ctx.crew_result
	monkeypatch.setattr("alfred.agents.crew.run_crew", _fake_run_crew)

	pipeline = _PipelineForTest(ctx)
	await pipeline._phase_run_crew()

	assert ctx.token_tracker is not None
	summary = ctx.token_tracker.get_summary()
	assert summary["total_tokens"] == 100 + 50 + 200 + 80
	assert summary["prompt_tokens"] == 300
	assert summary["completion_tokens"] == 130
	assert "Solution Architect" in summary["by_agent"]
	assert summary["by_agent"]["Solution Architect"]["total_tokens"] == 150
	assert summary["by_agent"]["Frappe Developer"]["total_tokens"] == 280


@pytest.mark.asyncio
async def test_run_crew_emits_usage_event_with_cost_estimate(monkeypatch):
	"""The ``usage`` WS event includes a ``cost`` block so the UI can
	render the dollar figure without hard-coding per-provider pricing."""
	send = AsyncMock()
	ctx = _make_ctx(
		mode="dev",
		agents=[_fake_agent("Frappe Developer", 1000, 500)],
		send=send,
	)

	async def _fake_run_crew(*_, **__):
		return ctx.crew_result
	monkeypatch.setattr("alfred.agents.crew.run_crew", _fake_run_crew)

	pipeline = _PipelineForTest(ctx)
	await pipeline._phase_run_crew()

	# A single ``usage`` send should land on the connection.
	usage_calls = [
		call for call in send.await_args_list
		if call.args and call.args[0].get("type") == "usage"
	]
	assert len(usage_calls) == 1
	payload = usage_calls[0].args[0]["data"]
	assert payload["total_tokens"] == 1500
	assert "cost" in payload
	assert payload["cost"]["total_tokens"] == 1500
	# Ollama is free in the cost table — pin that so the cost path
	# is exercised end-to-end without depending on a paid-provider
	# price drift.
	assert payload["cost"]["estimated_cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_run_crew_skips_token_tracker_for_non_dev_mode(monkeypatch):
	"""Chat / insights / plan modes don't run the multi-agent crew, so
	there's nothing per-agent to break down. The mixin must early-return
	without populating the tracker or emitting a stray ``usage`` event."""
	send = AsyncMock()
	ctx = _make_ctx(
		mode="chat",
		agents=[_fake_agent("ShouldNotMatter", 100, 100)],
		send=send,
	)

	async def _fake_run_crew(*_, **__):
		return ctx.crew_result
	monkeypatch.setattr("alfred.agents.crew.run_crew", _fake_run_crew)

	pipeline = _PipelineForTest(ctx)
	await pipeline._phase_run_crew()

	assert ctx.token_tracker is None
	send.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_crew_swallows_token_tracker_failure(monkeypatch):
	"""A broken ``_token_process`` (e.g. CrewAI internal change)
	must NOT abort an otherwise-successful crew run. The pipeline
	stays green; we just lose this one telemetry sample."""
	send = AsyncMock()

	bad_token_process = MagicMock()
	bad_token_process.get_summary.side_effect = AttributeError("crewai changed")
	bad_agent = SimpleNamespace(role="Architect", _token_process=bad_token_process)

	ctx = _make_ctx(mode="dev", agents=[bad_agent], send=send)

	async def _fake_run_crew(*_, **__):
		return ctx.crew_result
	monkeypatch.setattr("alfred.agents.crew.run_crew", _fake_run_crew)

	pipeline = _PipelineForTest(ctx)
	# Must not raise — the BLE001 guard on the telemetry block is the
	# whole point of this test.
	await pipeline._phase_run_crew()

	# Tracker may or may not be set depending on where the failure
	# landed — what matters is no exception propagated and the crew
	# result survived.
	assert ctx.crew_result is not None
