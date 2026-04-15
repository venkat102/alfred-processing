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
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from alfred.api.pipeline import (
	AgentPipeline,
	PipelineContext,
	StopSignal,
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
			"plan_check",
			"orchestrate",
			"enhance",
			"clarify",
			"resolve_mode",
			"build_crew",
			"run_crew",
			"post_crew",
		]
		assert AgentPipeline.PHASES == expected

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
