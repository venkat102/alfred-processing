"""Tests for the dedicated LLM thread pool (TD-H6 Phase 1)."""

from __future__ import annotations

import threading

from alfred.llm_client import _get_llm_executor


def test_executor_is_singleton():
	# Repeated calls return the same executor instance — don't want
	# one pool per caller, that defeats the isolation point.
	first = _get_llm_executor()
	second = _get_llm_executor()
	assert first is second


def test_executor_threads_named_for_visibility():
	# Named threads make jstack / ps output distinguish LLM workers.
	executor = _get_llm_executor()
	# Submit a no-op and capture the worker's thread name.
	done = threading.Event()
	captured: dict = {}

	def _sample():
		captured["name"] = threading.current_thread().name
		done.set()

	executor.submit(_sample)
	done.wait(timeout=5)
	assert captured["name"].startswith("alfred-llm")


def test_executor_max_workers_respects_setting(monkeypatch):
	# Clear module-level singleton so we rebuild with a custom setting.
	import alfred.llm_client as llm_client_mod
	llm_client_mod._llm_executor = None  # reset

	from alfred.config import get_settings
	if hasattr(get_settings, "cache_clear"):
		get_settings.cache_clear()
	monkeypatch.setenv("LLM_POOL_SIZE", "4")
	monkeypatch.setenv("API_SECRET_KEY", "test-key-for-pool-test")
	monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost")

	executor = llm_client_mod._get_llm_executor()
	assert executor._max_workers == 4

	# Cleanup — reset so downstream tests don't inherit this tiny pool.
	executor.shutdown(wait=False)
	llm_client_mod._llm_executor = None
	if hasattr(get_settings, "cache_clear"):
		get_settings.cache_clear()
