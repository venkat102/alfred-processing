"""Tests for the Phase 3 #12 pipeline state machine.

Covers:
  - PipelineContext.stop() sets the signal fields
  - AgentPipeline.PHASES order matches the expected linear flow
  - run() short-circuits after a phase calls ctx.stop()
  - _phase_sanitize blocks on rejected prompts and passes through on ok ones
  - _phase_resolve_mode precedence: plan_pipeline_mode > site_config > default
  - Unexpected exception in a phase is caught and sent as PIPELINE_ERROR
  - TimeoutError in a phase is caught and sent as PIPELINE_TIMEOUT
  - stop() error is emitted via conn.send after run() returns
  - Each phase is wrapped in a tracer span when tracing is enabled
"""

import asyncio
from contextlib import ExitStack

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from alfred.api.pipeline import (
	AgentPipeline,
	PipelineContext,
	StopSignal,
	_detect_drift,
)


def _run(coro):
	return asyncio.get_event_loop().run_until_complete(coro)


def _make_ctx(prompt="build a notification"):
	"""Build a context with a mock conn that records sent messages."""
	conn = MagicMock()
	conn.send = AsyncMock()
	conn.site_id = "test-site"
	conn.user = "tester@example.com"
	conn.roles = ["System Manager"]
	conn.site_config = {"llm_model": "ollama/llama3.1", "pipeline_mode": "lite"}
	conn.mcp_client = None

	# Fake websocket app state so _phase_load_state / _phase_plan_check don't crash
	conn.websocket = MagicMock()
	conn.websocket.app.state.redis = None
	conn.websocket.app.state.settings = MagicMock(
		ADMIN_PORTAL_URL="",
		ADMIN_SERVICE_KEY="",
	)

	return PipelineContext(
		conn=conn,
		conversation_id="conv-1",
		prompt=prompt,
	)


class TestPipelineContext:
	def test_stop_sets_signal(self):
		ctx = _make_ctx()
		ctx.stop(error="no go", code="BAD")
		assert ctx.should_stop is True
		assert ctx.stop_signal is not None
		assert ctx.stop_signal.error == "no go"
		assert ctx.stop_signal.code == "BAD"

	def test_stop_captures_extra_kwargs(self):
		ctx = _make_ctx()
		ctx.stop(error="over quota", code="PLAN_EXCEEDED", warning="near limit")
		assert ctx.stop_signal.extra["warning"] == "near limit"

	def test_initial_state(self):
		ctx = _make_ctx()
		assert ctx.should_stop is False
		assert ctx.stop_signal is None
		assert ctx.changes == []
		assert ctx.pipeline_mode == "full"


class TestPipelineOrder:
	def test_phases_in_expected_order(self):
		expected = [
			"sanitize",
			"load_state",
			"warmup",
			"plan_check",
			"orchestrate",
			"enhance",
			"clarify",
			"inject_kb",
			"resolve_mode",
			"build_crew",
			"run_crew",
			"post_crew",
		]
		assert AgentPipeline.PHASES == expected

	def test_inject_kb_sits_between_clarify_and_resolve_mode(self):
		"""Phase B invariant: auto-inject must run AFTER the clarified prompt
		is final, and BEFORE the crew is built. Shifting it breaks the
		guarantee that the banner is in front of the crew's task description."""
		phases = AgentPipeline.PHASES
		assert phases.index("inject_kb") == phases.index("clarify") + 1
		assert phases.index("inject_kb") + 1 == phases.index("resolve_mode")

	def test_every_phase_has_method(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)
		for name in AgentPipeline.PHASES:
			assert hasattr(pipeline, f"_phase_{name}"), f"missing _phase_{name}"


class TestStopShortCircuits:
	def test_stop_during_phase_skips_remainder(self):
		"""If a phase calls ctx.stop(), later phases are not executed."""
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		called: list[str] = []

		async def stop_phase():
			called.append("sanitize")
			ctx.stop(error="blocked by defense", code="PROMPT_BLOCKED")

		async def must_not_run():
			called.append("load_state")

		with patch.object(pipeline, "_phase_sanitize", side_effect=stop_phase), \
			 patch.object(pipeline, "_phase_load_state", side_effect=must_not_run):
			_run(pipeline.run())

		assert called == ["sanitize"]
		# Error was emitted
		sent = ctx.conn.send.call_args_list
		assert any(
			call[0][0]["type"] == "error"
			and call[0][0]["data"]["code"] == "PROMPT_BLOCKED"
			for call in sent
		)


class TestSanitizePhase:
	def test_allowed_prompt_does_not_stop(self):
		ctx = _make_ctx("Create a notification for Sales Order submission")
		pipeline = AgentPipeline(ctx)
		with patch(
			"alfred.defense.sanitizer.check_prompt",
			return_value={"allowed": True, "needs_review": False},
		):
			_run(pipeline._phase_sanitize())
		assert ctx.should_stop is False

	def test_rejected_prompt_triggers_stop(self):
		ctx = _make_ctx("malicious injection")
		pipeline = AgentPipeline(ctx)
		with patch(
			"alfred.defense.sanitizer.check_prompt",
			return_value={
				"allowed": False,
				"needs_review": False,
				"rejection_reason": "looks like an attack",
			},
		):
			_run(pipeline._phase_sanitize())
		assert ctx.should_stop is True
		assert ctx.stop_signal.code == "PROMPT_BLOCKED"
		assert "attack" in ctx.stop_signal.error

	def test_needs_review_uses_review_code(self):
		ctx = _make_ctx("borderline")
		pipeline = AgentPipeline(ctx)
		with patch(
			"alfred.defense.sanitizer.check_prompt",
			return_value={
				"allowed": False,
				"needs_review": True,
				"rejection_reason": "flagged for human review",
			},
		):
			_run(pipeline._phase_sanitize())
		assert ctx.stop_signal.code == "NEEDS_REVIEW"


class TestWarmupPhase:
	def _ctx_with_models(self, **extra):
		ctx = _make_ctx()
		ctx.conn.site_config = {
			"llm_model": "ollama/default",
			"llm_base_url": "http://fake-ollama:11434",
			**extra,
		}
		return ctx

	def test_no_warmup_when_single_model(self):
		ctx = self._ctx_with_models()
		pipeline = AgentPipeline(ctx)
		with patch("urllib.request.urlopen") as stub:
			_run(pipeline._phase_warmup())
		stub.assert_not_called()

	def test_warms_each_distinct_tier_model(self):
		ctx = self._ctx_with_models(
			llm_model_triage="ollama/gemma:2b",
			llm_model_reasoning="ollama/qwen3.5:latest",
			llm_model_agent="ollama/qwen2.5-coder:32b",
		)
		pipeline = AgentPipeline(ctx)
		with patch("urllib.request.urlopen") as stub:
			_run(pipeline._phase_warmup())
		# Three distinct tier models -> three warmup calls.
		assert stub.call_count == 3
		# Each call should hit /api/generate (not /api/chat) with keep_alive set.
		urls_called = []
		bodies = []
		for call in stub.call_args_list:
			req = call.args[0]
			urls_called.append(req.full_url)
			import json as _j
			bodies.append(_j.loads(req.data))
		for url in urls_called:
			assert url == "http://fake-ollama:11434/api/generate"
		for body in bodies:
			assert body["keep_alive"] == "10m"
			assert body["options"]["num_predict"] == 1

	def test_warmup_dedupes_identical_models_across_tiers(self):
		ctx = self._ctx_with_models(
			llm_model_triage="ollama/same-model",
			llm_model_reasoning="ollama/same-model",
			llm_model_agent="ollama/different",
		)
		pipeline = AgentPipeline(ctx)
		with patch("urllib.request.urlopen") as stub:
			_run(pipeline._phase_warmup())
		# Two distinct models, not three.
		assert stub.call_count == 2

	def test_warmup_swallows_network_failure(self):
		ctx = self._ctx_with_models(
			llm_model_triage="ollama/gemma:2b",
			llm_model_agent="ollama/qwen2.5-coder:32b",
		)
		pipeline = AgentPipeline(ctx)
		import urllib.error
		with patch(
			"urllib.request.urlopen",
			side_effect=urllib.error.URLError("ollama unreachable"),
		):
			# Must not raise; pipeline must not be stopped.
			_run(pipeline._phase_warmup())
		assert ctx.should_stop is False


class TestResolveModePhase:
	def test_plan_mode_beats_site_config(self):
		ctx = _make_ctx()
		ctx.plan_pipeline_mode = "lite"
		ctx.conn.site_config = {"pipeline_mode": "full"}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		assert ctx.pipeline_mode == "lite"
		assert ctx.pipeline_mode_source == "plan"

	def test_site_config_when_no_plan_override(self):
		ctx = _make_ctx()
		ctx.plan_pipeline_mode = None
		ctx.conn.site_config = {"pipeline_mode": "lite"}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		assert ctx.pipeline_mode == "lite"
		assert ctx.pipeline_mode_source == "site_config"

	def test_default_full_when_neither(self):
		ctx = _make_ctx()
		ctx.plan_pipeline_mode = None
		ctx.conn.site_config = {}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		assert ctx.pipeline_mode == "full"

	def test_rejects_invalid_mode(self):
		ctx = _make_ctx()
		ctx.plan_pipeline_mode = None
		ctx.conn.site_config = {"pipeline_mode": "banana"}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		assert ctx.pipeline_mode == "full"

	def test_resolve_mode_sends_started_event(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		sent_types = [
			call[0][0].get("type") for call in ctx.conn.send.call_args_list
		]
		assert "agent_status" in sent_types


class TestErrorBoundaries:
	def test_timeout_in_phase_becomes_pipeline_timeout(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		async def raise_timeout():
			raise asyncio.TimeoutError()

		with patch.object(pipeline, "_phase_sanitize", side_effect=raise_timeout):
			_run(pipeline.run())

		sent = ctx.conn.send.call_args_list
		assert any(
			call[0][0]["type"] == "error"
			and call[0][0]["data"]["code"] == "PIPELINE_TIMEOUT"
			for call in sent
		)

	def test_generic_exception_becomes_pipeline_error(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		async def boom():
			raise RuntimeError("something exploded")

		with patch.object(pipeline, "_phase_sanitize", side_effect=boom):
			_run(pipeline.run())

		sent = ctx.conn.send.call_args_list
		assert any(
			call[0][0]["type"] == "error"
			and call[0][0]["data"]["code"] == "PIPELINE_ERROR"
			and "something exploded" in call[0][0]["data"]["error"]
			for call in sent
		)

	def test_stop_signal_emitted_after_phase_loop(self):
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		async def early_stop():
			ctx.stop(error="no luck", code="TEST_CODE", detail="extra")

		with patch.object(pipeline, "_phase_sanitize", side_effect=early_stop), \
			 patch.object(pipeline, "_phase_load_state", new=AsyncMock()):
			_run(pipeline.run())

		sent = ctx.conn.send.call_args_list
		error_calls = [
			call for call in sent
			if call[0][0]["type"] == "error"
		]
		assert len(error_calls) == 1
		data = error_calls[0][0][0]["data"]
		assert data["code"] == "TEST_CODE"
		assert data["error"] == "no luck"
		assert data["detail"] == "extra"

	@pytest.mark.parametrize("failing_phase", AgentPipeline.PHASES)
	def test_every_phase_exception_is_caught_and_stops_pipeline(self, failing_phase):
		"""Inject a RuntimeError into each phase one at a time and verify:
		- the error is caught at run() level (no bubble to caller)
		- a PIPELINE_ERROR message is sent to the client
		- every phase AFTER the failing one is skipped (no silent continuation)
		"""
		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		async def boom():
			raise RuntimeError(f"{failing_phase} went boom")

		# Replace the failing phase with the raiser and every subsequent
		# phase with a sentinel we can assert was NOT called.
		phases = AgentPipeline.PHASES
		fail_idx = phases.index(failing_phase)
		patches = {failing_phase: boom}
		sentinels = {}
		for later in phases[fail_idx + 1:]:
			sentinels[later] = AsyncMock()
			patches[later] = sentinels[later]

		with ExitStack() as stack:
			for name, side in patches.items():
				if isinstance(side, AsyncMock):
					stack.enter_context(patch.object(pipeline, f"_phase_{name}", new=side))
				else:
					stack.enter_context(
						patch.object(pipeline, f"_phase_{name}", side_effect=side)
					)
			# All pre-failure phases also neutralised so we don't hit side effects
			for earlier in phases[:fail_idx]:
				if earlier != failing_phase:
					stack.enter_context(
						patch.object(pipeline, f"_phase_{earlier}", new=AsyncMock()),
					)
			_run(pipeline.run())

		# Pipeline emitted the error cleanly
		sent_types = [call[0][0].get("type") for call in ctx.conn.send.call_args_list]
		assert "error" in sent_types, f"No error message sent after {failing_phase} failed"
		error_payload = next(
			call[0][0]["data"] for call in ctx.conn.send.call_args_list
			if call[0][0].get("type") == "error"
		)
		assert error_payload["code"] == "PIPELINE_ERROR"
		assert failing_phase in error_payload["error"], (
			f"Error message should mention the failing phase: {error_payload['error']}"
		)
		# Downstream phases were NOT invoked
		for later, sentinel in sentinels.items():
			assert not sentinel.called, (
				f"Phase {later} ran after {failing_phase} raised - pipeline did not stop"
			)


class TestTracerIntegration:
	def test_phases_are_wrapped_in_spans_when_enabled(self):
		from alfred.obs.tracer import Tracer

		ctx = _make_ctx()
		pipeline = AgentPipeline(ctx)

		spans = []

		# Build a fresh tracer, register a capturing exporter, and patch the
		# global tracer used by pipeline.run().
		t = Tracer()
		t.enable()
		t.register_exporter(spans.append)

		async def noop():
			return None

		# Stub every phase to a no-op so only span creation is exercised.
		for name in AgentPipeline.PHASES:
			setattr(pipeline, f"_phase_{name}", noop)

		with patch("alfred.api.pipeline.tracer", t):
			_run(pipeline.run())

		span_names = [s["name"] for s in spans]
		for name in AgentPipeline.PHASES:
			assert f"pipeline.{name}" in span_names, f"missing span for {name}"


class TestOrchestratePhase:
	"""Covers the three-mode chat orchestrator phase.

	Behavior under test:
	  - Feature flag off: mode stays "dev", no LLM call, no skip
	  - Feature flag on: classify_mode is called, ctx.mode is set
	  - Chat mode short-circuits: chat handler runs, ctx.should_stop = True
	  - Dev mode continues: downstream phases are NOT gated
	  - Non-dev mode gates enhance/clarify/resolve_mode/build/run/post
	"""

	def test_flag_off_preserves_dev_default(self, monkeypatch):
		monkeypatch.delenv("ALFRED_ORCHESTRATOR_ENABLED", raising=False)
		ctx = _make_ctx("hi")
		pipeline = AgentPipeline(ctx)

		with patch("alfred.orchestrator.classify_mode") as classifier:
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "dev"
		assert ctx.should_stop is False
		classifier.assert_not_called()

	def test_flag_on_calls_classifier_and_sets_mode(self, monkeypatch):
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("add a priority field to Sales Order")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="dev",
				reason="fast_path match",
				confidence="high",
				source="fast_path",
			)

		with patch("alfred.orchestrator.classify_mode", side_effect=fake_classify):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "dev"
		assert ctx.orchestrator_reason == "fast_path match"
		assert ctx.orchestrator_source == "fast_path"
		assert ctx.should_stop is False

	def test_chat_mode_short_circuits(self, monkeypatch):
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("hi")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="chat",
				reason="greeting",
				confidence="high",
				source="fast_path",
			)

		async def fake_chat(**kwargs):
			return "Hi! How can I help?"

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.chat.handle_chat", side_effect=fake_chat):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "chat"
		assert ctx.should_stop is True
		assert ctx.chat_reply == "Hi! How can I help?"

		sent_types = [
			call[0][0].get("type") for call in ctx.conn.send.call_args_list
		]
		assert "mode_switch" in sent_types
		assert "chat_reply" in sent_types

	def test_chat_mode_handles_handler_exception(self, monkeypatch):
		"""Handler crash must not take down the pipeline - user gets a fallback."""
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("hi")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="chat", reason="greeting", confidence="high", source="fast_path"
			)

		async def broken_chat(**kwargs):
			raise RuntimeError("llm exploded")

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.chat.handle_chat", side_effect=broken_chat):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "chat"
		assert ctx.chat_reply is not None
		assert "trouble" in ctx.chat_reply.lower()

	def test_dev_mode_does_not_short_circuit(self, monkeypatch):
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("add a priority field")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="dev", reason="build verb", confidence="high", source="fast_path"
			)

		with patch("alfred.orchestrator.classify_mode", side_effect=fake_classify):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "dev"
		assert ctx.should_stop is False

	def test_insights_mode_short_circuits(self, monkeypatch):
		"""Phase B: insights mode runs the insights handler and stops the pipeline."""
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("what DocTypes do I have?")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision
		from alfred.state.conversation_memory import ConversationMemory

		ctx.conversation_memory = ConversationMemory(conversation_id="conv-1")

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="insights",
				reason="info query",
				confidence="high",
				source="fast_path",
			)

		async def fake_insights(**kwargs):
			return "You have 42 DocTypes in the HR module."

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.insights.handle_insights", side_effect=fake_insights):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "insights"
		assert ctx.should_stop is True
		assert ctx.insights_reply == "You have 42 DocTypes in the HR module."

		sent_types = [
			call[0][0].get("type") for call in ctx.conn.send.call_args_list
		]
		assert "mode_switch" in sent_types
		assert "insights_reply" in sent_types

		# Memory should have captured the Q/A pair for future Plan/Dev turns
		assert len(ctx.conversation_memory.insights_queries) == 1
		qa = ctx.conversation_memory.insights_queries[0]
		assert qa["q"] == "what DocTypes do I have?"
		assert "42 DocTypes" in qa["a"]

	def test_insights_mode_handles_handler_exception(self, monkeypatch):
		"""Insights handler crash must not take down the pipeline."""
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("what workflows do I have?")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="insights", reason="info query", confidence="high", source="fast_path"
			)

		async def broken(**kwargs):
			raise RuntimeError("mcp down")

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.insights.handle_insights", side_effect=broken):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "insights"
		assert ctx.insights_reply is not None
		assert "trouble" in ctx.insights_reply.lower() or "error" in ctx.insights_reply.lower()

	def test_plan_mode_short_circuits(self, monkeypatch):
		"""Phase C: plan mode runs the plan handler and stops the pipeline."""
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("how would we approach adding approval to Expense Claims?")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision
		from alfred.state.conversation_memory import ConversationMemory

		ctx.conversation_memory = ConversationMemory(conversation_id="conv-1")

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="plan",
				reason="design question",
				confidence="high",
				source="classifier",
			)

		fake_plan = {
			"title": "Approval for Expense Claims",
			"summary": "Two-step approval.",
			"steps": [{"order": 1, "action": "Create Workflow", "rationale": "x", "doctype": "Workflow"}],
			"doctypes_touched": ["Workflow"],
			"risks": [],
			"open_questions": [],
			"estimated_items": 1,
		}

		async def fake_plan_handler(**kwargs):
			return fake_plan

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.plan.handle_plan", side_effect=fake_plan_handler):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "plan"
		assert ctx.should_stop is True
		assert ctx.plan_doc == fake_plan

		sent_types = [
			call[0][0].get("type") for call in ctx.conn.send.call_args_list
		]
		assert "mode_switch" in sent_types
		assert "plan_doc" in sent_types

		# Memory should have the plan recorded as proposed + set as active
		assert ctx.conversation_memory.active_plan is not None
		assert ctx.conversation_memory.active_plan["title"] == fake_plan["title"]
		assert ctx.conversation_memory.active_plan["status"] == "proposed"
		assert len(ctx.conversation_memory.plan_documents) == 1

	def test_plan_handler_exception_produces_stub(self, monkeypatch):
		"""Plan handler crash must not take down the pipeline - user gets a stub."""
		monkeypatch.setenv("ALFRED_ORCHESTRATOR_ENABLED", "1")
		ctx = _make_ctx("design an approval flow")
		pipeline = AgentPipeline(ctx)

		from alfred.orchestrator import ModeDecision

		async def fake_classify(**kwargs):
			return ModeDecision(
				mode="plan", reason="design", confidence="high", source="classifier"
			)

		async def broken(**kwargs):
			raise RuntimeError("plan crew exploded")

		with patch(
			"alfred.orchestrator.classify_mode", side_effect=fake_classify
		), patch("alfred.handlers.plan.handle_plan", side_effect=broken):
			_run(pipeline._phase_orchestrate())

		assert ctx.mode == "plan"
		assert ctx.plan_doc is not None
		# Stub doc always has a title and a summary
		assert ctx.plan_doc["title"]
		assert ctx.plan_doc["summary"]


class TestPlanToDevHandoff:
	"""Phase C: Plan -> Dev handoff via ConversationMemory.active_plan."""

	def test_approval_phrase_flips_plan_to_approved(self):
		from alfred.state.conversation_memory import ConversationMemory

		ctx = _make_ctx("Approve and build the plan")
		ctx.mode = "dev"
		ctx.conversation_memory = ConversationMemory(conversation_id="c1")
		ctx.conversation_memory.add_plan_document(
			{"title": "P", "summary": "S", "steps": []}, status="proposed"
		)

		pipeline = AgentPipeline(ctx)
		pipeline._maybe_approve_active_plan()

		assert ctx.conversation_memory.active_plan["status"] == "approved"

	def test_non_approval_prompt_leaves_plan_proposed(self):
		from alfred.state.conversation_memory import ConversationMemory

		ctx = _make_ctx("Add a field to Sales Order")
		ctx.mode = "dev"
		ctx.conversation_memory = ConversationMemory(conversation_id="c1")
		ctx.conversation_memory.add_plan_document(
			{"title": "P", "summary": "S", "steps": []}, status="proposed"
		)

		pipeline = AgentPipeline(ctx)
		pipeline._maybe_approve_active_plan()

		# Plan is still proposed; unrelated dev prompts don't flip it
		assert ctx.conversation_memory.active_plan["status"] == "proposed"

	def test_built_plan_is_not_re_approved(self):
		from alfred.state.conversation_memory import ConversationMemory

		ctx = _make_ctx("Approve and build the plan")
		ctx.mode = "dev"
		ctx.conversation_memory = ConversationMemory(conversation_id="c1")
		ctx.conversation_memory.add_plan_document(
			{"title": "P", "summary": "S", "steps": []}, status="proposed"
		)
		ctx.conversation_memory.mark_active_plan_status("built")

		pipeline = AgentPipeline(ctx)
		pipeline._maybe_approve_active_plan()

		# Built plans stay built - no re-injection on later turns
		assert ctx.conversation_memory.active_plan["status"] == "built"

	def test_mark_built_after_dev_run(self):
		from alfred.state.conversation_memory import ConversationMemory

		ctx = _make_ctx()
		ctx.mode = "dev"
		ctx.conversation_memory = ConversationMemory(conversation_id="c1")
		ctx.conversation_memory.add_plan_document(
			{"title": "P", "summary": "S", "steps": []}, status="approved"
		)

		pipeline = AgentPipeline(ctx)
		pipeline._mark_active_plan_built_if_any()

		assert ctx.conversation_memory.active_plan["status"] == "built"

	def test_mark_built_noop_for_proposed_plan(self):
		from alfred.state.conversation_memory import ConversationMemory

		ctx = _make_ctx()
		ctx.mode = "dev"
		ctx.conversation_memory = ConversationMemory(conversation_id="c1")
		ctx.conversation_memory.add_plan_document(
			{"title": "P", "summary": "S", "steps": []}, status="proposed"
		)

		pipeline = AgentPipeline(ctx)
		pipeline._mark_active_plan_built_if_any()

		# Only approved plans get flipped to built
		assert ctx.conversation_memory.active_plan["status"] == "proposed"


class TestModeGating:
	"""Every dev-only phase must early-return when ctx.mode != 'dev'."""

	def test_enhance_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		pipeline = AgentPipeline(ctx)

		with patch("alfred.agents.prompt_enhancer.enhance_prompt") as enhancer:
			_run(pipeline._phase_enhance())
		enhancer.assert_not_called()

	def test_clarify_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		pipeline = AgentPipeline(ctx)

		with patch("alfred.api.websocket._clarify_requirements") as clarifier:
			_run(pipeline._phase_clarify())
		clarifier.assert_not_called()

	def test_resolve_mode_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		ctx.conn.site_config = {"pipeline_mode": "lite"}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_resolve_mode())
		# pipeline_mode should remain at the dataclass default, not get overwritten
		assert ctx.pipeline_mode == "full"

	def test_build_crew_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		pipeline = AgentPipeline(ctx)

		with patch("alfred.agents.crew.build_alfred_crew") as builder:
			_run(pipeline._phase_build_crew())
		builder.assert_not_called()

	def test_run_crew_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		pipeline = AgentPipeline(ctx)

		with patch("alfred.agents.crew.run_crew") as runner:
			_run(pipeline._phase_run_crew())
		runner.assert_not_called()

	def test_post_crew_gates_on_non_dev(self):
		ctx = _make_ctx()
		ctx.mode = "chat"
		ctx.crew_result = {"status": "completed", "result": "ignored"}
		pipeline = AgentPipeline(ctx)
		_run(pipeline._phase_post_crew())
		# No changeset extraction, no error emission
		assert ctx.changes == []
		assert ctx.should_stop is False


class TestDetectDrift:
	"""Regression tests for the Sales Order training-data bleed.

	The Developer agent running on qwen2.5-coder:32b sometimes slips
	out of the task structure and regurgitates documentation about
	Sales Order (the most-cited DocType in its training data) even
	when the user asked about something else entirely. `_detect_drift`
	catches that class of output before extraction / rescue / UI so
	the user gets a specific error instead of a wall of off-topic prose.
	"""

	def test_clean_json_is_not_drift(self):
		result = '[{"op": "create", "doctype": "Server Script", "data": {"reference_doctype": "Employee"}}]'
		prompt = "add a validation to Employee doctype"
		assert _detect_drift(result, prompt) is None

	def test_employee_validation_sales_order_dump_is_drift(self):
		"""Exact failure mode: user asked for Employee validation, agent
		dumped Sales Order documentation."""
		result = (
			"The provided JSON structure describes the metadata for a custom "
			"document type in an ERPNext or Frappe framework, specifically for a "
			"Sales Order. Module: Selling. Fields: name, customer_name, "
			"transaction_date, delivery_date, status, items, total, grand_total, "
			"taxes_and_charges, sales_team."
		)
		prompt = (
			"i want to add a validation to the employee doctype, for any "
			"employee only above the age of 24 can be created"
		)
		reason = _detect_drift(result, prompt)
		assert reason is not None
		# Must mention a concrete training-data smell in the reason
		assert "customer_name" in reason or "taxes_and_charges" in reason or "sales_team" in reason or "Sales Order" in reason

	def test_doc_mode_phrase_alone_is_drift(self):
		result = (
			"The provided JSON structure represents a notification with "
			"various fields like subject, event, channel, recipients, message. "
			"It targets the Leave Application doctype and fires on Submit. "
			"Here's a breakdown of the fields you'd use in this case."
			+ " filler " * 200  # push length > 1500
		)
		prompt = "notify the approver when a leave is submitted"
		assert _detect_drift(result, prompt) is not None

	def test_long_pure_prose_is_drift(self):
		result = "This describes a Frappe customization. " * 100
		prompt = "add a field"
		reason = _detect_drift(result, prompt)
		assert reason is not None
		assert "prose" in reason.lower() or "long" in reason.lower()

	def test_short_prose_is_not_drift(self):
		"""Short explanations that happen to lack JSON aren't drift -
		they're just unfortunate. Let extraction handle them."""
		result = "I'll create a notification for you."
		prompt = "email me when a leave is submitted"
		assert _detect_drift(result, prompt) is None

	def test_mentioning_training_data_field_is_drift(self):
		result = (
			'[{"op": "create", "doctype": "Server Script", "data": '
			'{"reference_doctype": "Sales Order", "customer_name": "X"}}]'
		)
		prompt = "validate Employee age"
		reason = _detect_drift(result, prompt)
		assert reason is not None
		assert "customer_name" in reason

	def test_user_mention_of_sales_order_is_not_drift(self):
		"""If the user DID ask about Sales Order, mentioning it isn't drift."""
		result = (
			"I'll create a Server Script on Sales Order to validate the grand_total."
		)
		prompt = "validate that Sales Order grand_total is not zero"
		assert _detect_drift(result, prompt) is None

	def test_multiple_foreign_doctypes_is_drift(self):
		result = (
			"For this request I would use Sales Invoice, Purchase Invoice, "
			"Payment Entry, and Journal Entry to track the payments."
		)
		prompt = "add a priority field to ToDo"
		reason = _detect_drift(result, prompt)
		assert reason is not None

	def test_empty_result_is_not_drift(self):
		assert _detect_drift("", "validate Employee age") is None
		assert _detect_drift(None, "validate Employee age") is None


# ──────────────────────────────────────────────────────────────────────
# Phase B: inject_kb auto-injection tests
# ──────────────────────────────────────────────────────────────────────


def _kb_entry(
	entry_id: str = "server_script_no_imports",
	kind: str = "rule",
	title: str = "Server Scripts cannot use import",
	summary: str = "RestrictedPython has no __import__.",
	body: str = "- import is forbidden in Server Script bodies.",
	mode: str = "keyword",
):
	"""Shape-compatible stand-in for a single KB entry returned by
	fkb.search_hybrid. Includes the `_mode` tag the hybrid function stamps
	onto each hit so the pipeline's banner header includes it."""
	return {
		"id": entry_id, "kind": kind, "title": title,
		"summary": summary, "body": body,
		"_score": 18, "_mode": mode,
	}


class TestInjectKB:
	"""Phase B + B.5 + C: inject_kb pipeline phase.

	After Phase C, the FKB retrieval layer no longer goes through MCP -
	it's a direct call into alfred.knowledge.fkb.search_hybrid on the
	processing side. These tests patch that symbol directly instead of
	the MCP mock (which only matters for site-recon now).
	"""

	def _setup_ctx(self, mcp_client_present=True):
		"""Build a Dev-mode context. MCP client is wired by default because
		site-recon (Phase B.5) still uses MCP; pass mcp_client_present=False
		to simulate a no-MCP environment and verify FKB still works."""
		ctx = _make_ctx(prompt="add a validation to Employee age <24")
		ctx.mode = "dev"
		ctx.enhanced_prompt = (
			"The user wants a validation on the Employee DocType that throws "
			"when date_of_birth indicates age < 24. Use a Server Script."
		)

		if mcp_client_present:
			mcp_client = MagicMock()
			# Default: site-detail returns not_found so site-recon no-ops.
			mcp_client.call_tool = AsyncMock(
				return_value={"error": "not_found", "message": "DocType not found"}
			)
			ctx.conn.mcp_client = mcp_client
		else:
			ctx.conn.mcp_client = None
		return ctx

	def _run_with_fkb(self, ctx, fkb_hits=None, fkb_raises=None):
		"""Run _phase_inject_kb with alfred.knowledge.fkb.search_hybrid
		patched to return `fkb_hits` (or raise `fkb_raises`). Returns the
		patch mock so callers can assert on call args."""
		from alfred.knowledge import fkb as _fkb_mod

		side_effect = fkb_raises if fkb_raises is not None else None
		return_value = fkb_hits if fkb_raises is None else None

		with patch.object(
			_fkb_mod, "search_hybrid",
			side_effect=side_effect,
			return_value=return_value,
		) as mock_fkb:
			pipeline = AgentPipeline(ctx)
			_run(pipeline._phase_inject_kb())
			return mock_fkb

	def test_skipped_when_not_dev_mode(self):
		"""Plan / Insights / Chat don't produce code, so no KB injection."""
		for mode in ("plan", "insights", "chat"):
			ctx = self._setup_ctx()
			ctx.mode = mode
			original = ctx.enhanced_prompt
			mock_fkb = self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])
			assert ctx.enhanced_prompt == original, f"{mode} mode should skip"
			assert ctx.injected_kb == []
			# FKB retrieval should not even run in non-dev modes.
			assert mock_fkb.call_count == 0

	def test_no_mcp_client_still_runs_fkb(self):
		"""Phase C: FKB retrieval is local and doesn't need MCP. Only the
		site-recon portion is gated on mcp_client. Verify that without MCP,
		FKB still injects but site-recon doesn't run."""
		ctx = self._setup_ctx(mcp_client_present=False)
		self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])
		# FKB injected
		assert ctx.injected_kb == ["server_script_no_imports"]
		assert "FRAPPE KB CONTEXT" in ctx.enhanced_prompt
		# Site-recon skipped
		assert ctx.injected_site_state == {}

	def test_skipped_when_empty_prompt(self):
		ctx = self._setup_ctx()
		ctx.enhanced_prompt = ""
		mock_fkb = self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])
		assert ctx.enhanced_prompt == ""
		assert ctx.injected_kb == []
		# Empty-prompt gate runs BEFORE FKB call.
		assert mock_fkb.call_count == 0

	def test_skipped_when_stop_signal_set(self):
		ctx = self._setup_ctx()
		ctx.should_stop = True
		original = ctx.enhanced_prompt
		mock_fkb = self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])
		assert ctx.enhanced_prompt == original
		assert mock_fkb.call_count == 0

	def test_injects_banner_on_single_match(self):
		"""Happy path: fkb.search_hybrid returns one entry, banner is prepended."""
		ctx = self._setup_ctx()
		original = ctx.enhanced_prompt
		self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])

		assert ctx.injected_kb == ["server_script_no_imports"]
		assert ctx.enhanced_prompt != original
		assert "FRAPPE KB CONTEXT" in ctx.enhanced_prompt
		assert "server_script_no_imports" in ctx.enhanced_prompt
		assert "Server Scripts cannot use import" in ctx.enhanced_prompt
		# Original prompt must still be present, AFTER the banner.
		banner_end = ctx.enhanced_prompt.index("USER REQUEST (interpret this verbatim)")
		orig_start = ctx.enhanced_prompt.index(original)
		assert banner_end < orig_start

	def test_banner_includes_mode_tag(self):
		"""Phase C: the hybrid retrieval tags each hit with _mode ('keyword'
		or 'semantic'). The banner includes this tag in the per-entry header
		so traces can tell which retriever won each slot."""
		ctx = self._setup_ctx()
		self._run_with_fkb(ctx, fkb_hits=[_kb_entry(mode="semantic")])
		# The per-entry header reads "[rule: <id> via semantic] <title>"
		assert "via semantic" in ctx.enhanced_prompt

	def test_injects_multiple_entries(self):
		entries = [
			_kb_entry(entry_id="server_script_no_imports"),
			_kb_entry(entry_id="minimal_change_principle",
			          title="Pick smallest Frappe primitive"),
			_kb_entry(entry_id="custom_field_vs_new_doctype",
			          title="Extend with Custom Field"),
		]
		ctx = self._setup_ctx()
		self._run_with_fkb(ctx, fkb_hits=entries)

		assert ctx.injected_kb == [
			"server_script_no_imports",
			"minimal_change_principle",
			"custom_field_vs_new_doctype",
		]
		for entry in entries:
			assert entry["title"] in ctx.enhanced_prompt

	def test_no_inject_on_empty_hits(self):
		"""search_hybrid returning [] -> no banner, no crash."""
		ctx = self._setup_ctx()
		original = ctx.enhanced_prompt
		self._run_with_fkb(ctx, fkb_hits=[])
		assert ctx.enhanced_prompt == original
		assert ctx.injected_kb == []

	def test_no_inject_when_fkb_raises(self):
		"""Infrastructure failure in search_hybrid -> swallow, pipeline continues."""
		ctx = self._setup_ctx()
		original = ctx.enhanced_prompt
		self._run_with_fkb(ctx, fkb_raises=RuntimeError("embedding model crashed"))
		assert ctx.enhanced_prompt == original
		assert ctx.injected_kb == []

	def test_fkb_called_with_enhanced_prompt(self):
		"""search_hybrid receives the enhanced_prompt as query and k=3."""
		ctx = self._setup_ctx()
		source_prompt = ctx.enhanced_prompt
		mock_fkb = self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])

		assert mock_fkb.call_count == 1
		args, kwargs = mock_fkb.call_args
		# First positional arg is the query; k=3 is passed as kwarg.
		assert args[0] == source_prompt
		assert kwargs.get("k") == 3

	def test_malformed_entry_is_skipped(self):
		ctx = self._setup_ctx()
		self._run_with_fkb(ctx, fkb_hits=[
			"not a dict",
			_kb_entry(entry_id="server_script_no_imports"),
			42,
		])
		assert ctx.injected_kb == ["server_script_no_imports"]
		assert "server_script_no_imports" in ctx.enhanced_prompt


# ──────────────────────────────────────────────────────────────────────
# Phase B.5: target extraction + site reconnaissance tests
# ──────────────────────────────────────────────────────────────────────

from alfred.api.pipeline import (  # noqa: E402
	_extract_target_doctypes,
	_render_site_state_block,
	_site_detail_has_artefacts,
)


def _site_detail(
	doctype="Employee",
	workflows=None,
	server_scripts=None,
	custom_fields=None,
	notifications=None,
	client_scripts=None,
):
	"""Shape-compatible stand-in for get_site_customization_detail responses."""
	return {
		"doctype": doctype,
		"workflows": workflows or [],
		"server_scripts": server_scripts or [],
		"custom_fields": custom_fields or [],
		"notifications": notifications or [],
		"client_scripts": client_scripts or [],
	}


class TestExtractTargetDoctypes:
	def test_picks_single_doctype_from_validation_prompt(self):
		prompt = (
			"The user wants a validation on the Employee DocType that throws "
			"when date_of_birth indicates age < 24."
		)
		targets = _extract_target_doctypes(prompt)
		assert "Employee" in targets

	def test_filters_common_english_words(self):
		"""Words like 'Python', 'Draft', 'Frappe' must not be treated as targets
		even when they start with a capital letter."""
		prompt = "Python Draft Frappe User Note"
		targets = _extract_target_doctypes(prompt)
		# All of these are in _NON_DOCTYPE_CAPITALIZED or too short.
		for noise in ("Python", "Draft", "Frappe", "User"):
			assert noise not in targets

	def test_respects_limit(self):
		prompt = (
			"I want to touch Employee, Sales Order, Purchase Order, Leave "
			"Application, and Material Request."
		)
		targets = _extract_target_doctypes(prompt, limit=2)
		assert len(targets) == 2
		# First-seen ordering preserved
		assert targets[0] == "Employee"

	def test_dedups_case_sensitive(self):
		prompt = "Employee here, Employee there, Employee everywhere"
		targets = _extract_target_doctypes(prompt)
		assert targets == ["Employee"]

	def test_skips_short_single_words(self):
		"""A short single-word capitalised token (e.g. 'HR', 'API') is noise."""
		targets = _extract_target_doctypes("ToDo ok", limit=2)
		# "ToDo" is 4 chars, below the 6-char single-word threshold.
		assert "ToDo" not in targets

	def test_multi_word_candidate_kept_even_if_each_word_is_short(self):
		"""Multi-word candidates pass the short-word filter since the hyphen
		between words makes them specific enough to be a real DocType."""
		# "Work Order" is a real DocType; enhanced_prompts usually start with
		# a noise word like 'The' or 'This' which would greedy-match past the
		# target, so construct a prompt where the target stands alone.
		assert _extract_target_doctypes(
			"add priority to Work Order records"
		) == ["Work Order"]

	def test_empty_prompt(self):
		assert _extract_target_doctypes("") == []
		assert _extract_target_doctypes(None) == []


class TestSiteDetailHasArtefacts:
	def test_empty_detail_returns_false(self):
		assert _site_detail_has_artefacts({}) is False
		assert _site_detail_has_artefacts(_site_detail()) is False

	def test_non_dict_returns_false(self):
		assert _site_detail_has_artefacts(None) is False
		assert _site_detail_has_artefacts("oops") is False

	def test_any_non_empty_list_returns_true(self):
		assert _site_detail_has_artefacts(
			_site_detail(custom_fields=[{"fieldname": "x"}])
		) is True
		assert _site_detail_has_artefacts(
			_site_detail(workflows=[{"name": "W"}])
		) is True
		assert _site_detail_has_artefacts(
			_site_detail(server_scripts=[{"name": "S"}])
		) is True


class TestRenderSiteStateBlock:
	def test_empty_artefacts_renders_safe_fallback(self):
		block = _render_site_state_block("Employee", _site_detail(), budget=2000)
		assert 'SITE STATE FOR "Employee"' in block
		assert "(no major artefacts)" in block

	def test_workflow_renders_states_and_transitions(self):
		detail = _site_detail(workflows=[{
			"name": "Employee Approval",
			"is_active": 1,
			"workflow_state_field": "workflow_state",
			"states": [
				{"state": "Draft", "doc_status": "0", "allow_edit": "Employee"},
				{"state": "Approved", "doc_status": "1", "allow_edit": "HR"},
			],
			"transitions": [
				{"state": "Draft", "action": "Submit",
				 "next_state": "Approved", "allowed": "HR Manager"},
			],
		}])
		block = _render_site_state_block("Employee", detail, budget=2000)
		assert "Workflow: Employee Approval (active" in block
		assert "Draft [Employee]" in block
		assert "Draft --Submit--> Approved" in block

	def test_server_script_body_is_indented(self):
		detail = _site_detail(server_scripts=[{
			"name": "Validate DoJ",
			"script_type": "DocType Event",
			"doctype_event": "Before Save",
			"disabled": 0,
			"script": "if not doc.date_of_joining:\n    frappe.throw('required')",
		}])
		block = _render_site_state_block("Employee", detail, budget=2000)
		assert "Server Script: Validate DoJ" in block
		assert "(Before Save, enabled)" in block
		# Indented body preview
		assert "    if not doc.date_of_joining:" in block

	def test_budget_cap_adds_truncation_footer(self):
		"""When the per-target budget is too small for everything, the lowest
		priority artefact gets dropped and a '(more artefacts omitted...)'
		footer is appended."""
		# Workflow renders first and takes most of a small budget; client
		# scripts should be dropped.
		detail = _site_detail(
			workflows=[{
				"name": "W",
				"is_active": 1,
				"workflow_state_field": "workflow_state",
				"states": [{"state": "Draft", "doc_status": "0", "allow_edit": "A"}],
				"transitions": [{"state": "Draft", "action": "Go",
				                  "next_state": "Done", "allowed": "A"}],
			}],
			client_scripts=[{"name": "C1", "view": "Form", "enabled": 1}],
		)
		block = _render_site_state_block("Employee", detail, budget=120)
		assert "more artefacts omitted" in block or "(no major artefacts)" in block


class TestInjectKBSiteRecon:
	"""Phase B.5 + C: site reconnaissance combined with hybrid FKB retrieval.

	After Phase C, FKB retrieval is local (alfred.knowledge.fkb.search_hybrid).
	Site reconnaissance still goes through MCP. These tests patch both
	independently so FKB hits and site-detail results can be controlled
	separately."""

	def _setup_ctx(self, site_results=None, site_raises=None):
		"""Build a Dev-mode context with an MCP client that answers per-
		DocType site-detail based on the `doctype` arg.

		`site_results` is a dict {doctype -> detail_dict}. Unknown doctypes
		get {"error": "not_found"}. If `site_raises` is set, site-detail
		calls raise that exception. FKB behaviour is controlled via the
		fkb.search_hybrid patch in _run_with_fkb."""
		ctx = _make_ctx(prompt="add a validation to Employee")
		ctx.mode = "dev"
		ctx.enhanced_prompt = (
			"The user wants a validation on the Employee DocType that throws "
			"when date_of_birth indicates age < 24."
		)

		mcp_client = MagicMock()
		site_results = site_results or {}

		async def fake_call_tool(name, args=None):
			args = args or {}
			# Site-recon is the only MCP call inject_kb makes after Phase C.
			if name == "get_site_customization_detail":
				if site_raises:
					raise site_raises
				doctype = args.get("doctype")
				if doctype in site_results:
					return site_results[doctype]
				return {"error": "not_found",
				        "message": f"DocType {doctype!r} not found"}
			# FKB used to go through MCP before Phase C; fall through to a
			# benign empty response in case a test still triggers one.
			return {"error": "unknown_tool"}

		mcp_client.call_tool = AsyncMock(side_effect=fake_call_tool)
		ctx.conn.mcp_client = mcp_client
		return ctx, mcp_client

	def _run_with_fkb(self, ctx, fkb_hits=None, fkb_raises=None):
		"""Run _phase_inject_kb with alfred.knowledge.fkb.search_hybrid patched."""
		from alfred.knowledge import fkb as _fkb_mod

		side_effect = fkb_raises if fkb_raises is not None else None
		return_value = fkb_hits if fkb_raises is None else None

		with patch.object(
			_fkb_mod, "search_hybrid",
			side_effect=side_effect,
			return_value=return_value,
		):
			pipeline = AgentPipeline(ctx)
			_run(pipeline._phase_inject_kb())

	def test_extracts_target_and_calls_site_detail(self):
		"""DocType mentioned in the prompt triggers a site-detail MCP call."""
		ctx, mcp = self._setup_ctx(
			site_results={"Employee": _site_detail(
				custom_fields=[{"fieldname": "emp_code", "fieldtype": "Data",
				                "label": "Employee Code", "reqd": 1}],
			)},
		)
		self._run_with_fkb(ctx, fkb_hits=[])

		# Only site-detail goes through MCP after Phase C. FKB is local.
		call_names = [c.args[0] for c in mcp.call_tool.await_args_list]
		assert "get_site_customization_detail" in call_names
		assert "lookup_frappe_knowledge" not in call_names, (
			"FKB should no longer go through MCP (Phase C)"
		)
		site_args = [c.args[1] for c in mcp.call_tool.await_args_list
		              if c.args[0] == "get_site_customization_detail"]
		assert any(a.get("doctype") == "Employee" for a in site_args)

	def test_injects_site_state_block_when_artefacts_exist(self):
		ctx, _ = self._setup_ctx(
			site_results={"Employee": _site_detail(
				server_scripts=[{
					"name": "Validate DoJ",
					"script_type": "DocType Event",
					"doctype_event": "Before Save",
					"disabled": 0,
					"script": "if not doc.date_of_joining: frappe.throw('x')",
				}],
			)},
		)
		self._run_with_fkb(ctx, fkb_hits=[])

		assert "Employee" in ctx.injected_site_state
		assert 'SITE STATE FOR "Employee"' in ctx.enhanced_prompt
		assert "Validate DoJ" in ctx.enhanced_prompt
		assert "if not doc.date_of_joining" in ctx.enhanced_prompt

	def test_skips_site_block_when_doctype_has_no_artefacts(self):
		ctx, _ = self._setup_ctx(site_results={"Employee": _site_detail()})
		self._run_with_fkb(ctx, fkb_hits=[])
		assert ctx.injected_site_state == {}
		assert 'SITE STATE FOR "Employee"' not in ctx.enhanced_prompt

	def test_skips_site_block_when_doctype_unknown(self):
		"""Site-detail returns not_found for everything → no site block. With
		empty FKB hits, no banner is injected at all."""
		ctx, _ = self._setup_ctx(site_results={})
		original = ctx.enhanced_prompt
		self._run_with_fkb(ctx, fkb_hits=[])

		assert ctx.injected_site_state == {}
		assert ctx.enhanced_prompt == original

	def test_site_detail_failure_does_not_break_pipeline(self):
		"""Site-detail MCP exception is swallowed; FKB still injects."""
		ctx, _ = self._setup_ctx(site_raises=RuntimeError("network"))
		self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])

		assert ctx.injected_kb == ["server_script_no_imports"]
		assert "FRAPPE KB CONTEXT" in ctx.enhanced_prompt
		assert ctx.injected_site_state == {}

	def test_fkb_failure_does_not_break_site_recon(self):
		"""Symmetric: FKB raising still allows site-recon to inject."""
		ctx, _ = self._setup_ctx(
			site_results={"Employee": _site_detail(
				custom_fields=[{"fieldname": "x", "fieldtype": "Data", "label": "X"}],
			)},
		)
		self._run_with_fkb(ctx, fkb_raises=RuntimeError("embedding failed"))

		assert ctx.injected_kb == []
		assert "FRAPPE KB CONTEXT" not in ctx.enhanced_prompt
		assert "Employee" in ctx.injected_site_state
		assert 'SITE STATE FOR "Employee"' in ctx.enhanced_prompt

	def test_site_state_placed_after_fkb_and_before_user_request(self):
		"""Ordering invariant: FKB block -> SITE STATE block -> USER REQUEST."""
		ctx, _ = self._setup_ctx(
			site_results={"Employee": _site_detail(
				custom_fields=[{"fieldname": "x", "fieldtype": "Data", "label": "X"}],
			)},
		)
		self._run_with_fkb(ctx, fkb_hits=[_kb_entry()])

		text = ctx.enhanced_prompt
		fkb_pos = text.index("FRAPPE KB CONTEXT")
		site_pos = text.index('SITE STATE FOR "Employee"')
		user_pos = text.index("USER REQUEST (interpret this verbatim)")
		assert fkb_pos < site_pos < user_pos

	def test_non_dev_mode_skips_both_retrievals(self):
		"""Plan/Insights/Chat must make zero MCP calls and zero FKB calls."""
		ctx, mcp = self._setup_ctx(
			site_results={"Employee": _site_detail(
				server_scripts=[{"name": "x", "script_type": "DocType Event",
				                  "doctype_event": "Before Save", "disabled": 0,
				                  "script": "pass"}],
			)},
		)
		ctx.mode = "plan"
		# Patch fkb.search_hybrid and assert it wasn't called either.
		from alfred.knowledge import fkb as _fkb_mod
		with patch.object(_fkb_mod, "search_hybrid", return_value=[_kb_entry()]) as fm:
			pipeline = AgentPipeline(ctx)
			_run(pipeline._phase_inject_kb())

			assert mcp.call_tool.await_count == 0
			assert fm.call_count == 0
		assert ctx.injected_kb == []
		assert ctx.injected_site_state == {}
