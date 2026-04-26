"""Tests for alfred.obs.tasks.spawn_logged."""

from __future__ import annotations

import asyncio
import logging

import pytest

from alfred.obs.tasks import spawn_logged


@pytest.mark.asyncio
async def test_returns_normal_task_for_clean_coro():
	"""A coroutine that returns normally yields a task whose result is available."""
	async def ok():
		return 42

	task = spawn_logged(ok(), name="test-ok")
	result = await task
	assert result == 42


@pytest.mark.asyncio
async def test_exception_logged_at_error(caplog):
	"""A coroutine that raises: the task's done-callback logs ERROR."""
	async def boom():
		raise RuntimeError("intentional")

	with caplog.at_level(logging.ERROR, logger="alfred.obs.tasks"):
		task = spawn_logged(boom(), name="test-boom")
		# Wait for the task to finish. Swallow the exception here (we are
		# asserting the logger caught it; awaiting re-raises).
		try:
			await task
		except RuntimeError:
			pass

	matching = [r for r in caplog.records if "test-boom" in r.getMessage()]
	assert matching, "expected an ERROR log mentioning the task name"
	assert matching[0].levelno == logging.ERROR
	assert "RuntimeError" in matching[0].getMessage()


@pytest.mark.asyncio
async def test_cancelled_logged_at_debug_not_error(caplog):
	"""Cancellation is expected on disconnect and must not noise ERROR logs."""
	async def long_sleep():
		await asyncio.sleep(10)

	with caplog.at_level(logging.DEBUG, logger="alfred.obs.tasks"):
		task = spawn_logged(long_sleep(), name="test-cancel")
		await asyncio.sleep(0)  # let the task start
		task.cancel()
		with pytest.raises(asyncio.CancelledError):
			await task

	errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
	assert errors == [], f"cancel should not log ERROR, got: {[r.getMessage() for r in errors]}"
	debug_matches = [
		r for r in caplog.records
		if r.levelno == logging.DEBUG and "test-cancel" in r.getMessage()
	]
	assert debug_matches, "expected a DEBUG log for the cancelled task"


@pytest.mark.asyncio
async def test_task_name_set_on_returned_task():
	"""The name passed in is applied to the task for debuggability."""
	async def ok():
		pass

	task = spawn_logged(ok(), name="my-task-name")
	await task
	assert task.get_name() == "my-task-name"
