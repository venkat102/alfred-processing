"""Unit-level tests for #FLOW1 (WS resume event replay).

Before this feature, a client that lost its WS connection mid-pipeline
had to re-send the prompt on reconnect - any agent_status / changeset /
info events emitted while disconnected were silently dropped. The
`resume` message type existed in docs + client event_map but was a
server-side no-op.

These tests drive the ConnectionState.send() persist path + the
resume message handler directly against a mocked WebSocket and an
in-memory fake store so they run everywhere (no Redis required).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.api.websocket import ConnectionState, _handle_custom_message


class _FakeStore:
	"""Minimal in-memory stand-in for StateStore for resume testing.

	Keyed the same way StateStore is (site_id, conversation_id) ->
	list of event dicts. Preserves ordering, which is the whole
	point of a stream.
	"""

	def __init__(self) -> None:
		self._streams: dict[tuple[str, str], list[dict]] = {}
		self._counter = 0

	async def push_event(self, site_id: str, conversation_id: str, event: dict) -> str:
		self._counter += 1
		entry_id = f"{self._counter}-0"
		self._streams.setdefault((site_id, conversation_id), []).append({
			"id": entry_id,
			"data": event,
		})
		return entry_id

	async def get_events(self, site_id: str, conversation_id: str, since_id: str = "0") -> list[dict]:
		return list(self._streams.get((site_id, conversation_id), []))


def _make_conn(*, with_store: bool = True, conversation_id: str = "conv-1") -> ConnectionState:
	ws = MagicMock()
	ws.send_json = AsyncMock()
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.redis = MagicMock()
	store = _FakeStore() if with_store else None
	return ConnectionState(
		websocket=ws, site_id="t.site", user="u@x", roles=[],
		site_config={}, conversation_id=conversation_id, store=store,
	)


@pytest.mark.asyncio
async def test_send_persists_user_visible_messages():
	conn = _make_conn()
	await conn.send({"msg_id": "m1", "type": "changeset", "data": {"foo": "bar"}})
	events = await conn.store.get_events("t.site", "conv-1")
	assert len(events) == 1
	assert events[0]["data"]["msg_id"] == "m1"


@pytest.mark.asyncio
async def test_send_skips_transport_types():
	conn = _make_conn()
	# None of these should land in the stream - they're transport / meta.
	for mt in ("ack", "ping", "mcp_response", "echo"):
		await conn.send({"msg_id": f"m-{mt}", "type": mt, "data": {}})
	events = await conn.store.get_events("t.site", "conv-1")
	assert events == []


@pytest.mark.asyncio
async def test_send_persists_info_and_error_codes():
	# These are user-facing event types introduced in recent fixes.
	# Resume MUST replay them.
	conn = _make_conn()
	await conn.send({"msg_id": "m1", "type": "info", "data": {"code": "MEMORY_SAVE_FAILED"}})
	await conn.send({"msg_id": "m2", "type": "error", "data": {"code": "RATE_LIMIT"}})
	events = await conn.store.get_events("t.site", "conv-1")
	assert [e["data"]["type"] for e in events] == ["info", "error"]


@pytest.mark.asyncio
async def test_send_no_op_when_store_missing():
	conn = _make_conn(with_store=False)
	# Must not crash - the ws.send_json still goes through.
	await conn.send({"msg_id": "m1", "type": "changeset", "data": {}})
	conn.websocket.send_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_replays_events_after_last_msg_id():
	conn = _make_conn()
	# Seed: 4 events have been sent and persisted.
	for i in range(1, 5):
		await conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {"i": i}})
	# Reset the WS mock so we only see resume-path sends.
	conn.websocket.send_json.reset_mock()

	# Client reconnects saying "I last saw m2".
	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {"last_msg_id": "m2"}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)

	# Only m3 and m4 should have been replayed.
	sent = [call.args[0] for call in conn.websocket.send_json.await_args_list]
	msg_ids = [s["msg_id"] for s in sent]
	assert msg_ids == ["m3", "m4"]


@pytest.mark.asyncio
async def test_resume_replays_everything_when_last_msg_id_not_found():
	# Stream window has rolled past the client's last_msg_id, or the
	# client never actually saw it. Replay the whole window - the
	# client dedupes by msg_id.
	conn = _make_conn()
	for i in range(1, 4):
		await conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {"i": i}})
	conn.websocket.send_json.reset_mock()

	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {"last_msg_id": "m-nonexistent"}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)

	sent = [call.args[0]["msg_id"] for call in conn.websocket.send_json.await_args_list]
	assert sent == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_resume_no_op_when_last_msg_id_missing():
	conn = _make_conn()
	for i in range(1, 3):
		await conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {}})
	conn.websocket.send_json.reset_mock()

	# No last_msg_id in the resume payload - replay nothing. Replaying
	# everything by default would dump thousands of events on a client
	# that doesn't have an anchor yet.
	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)
	conn.websocket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_resume_no_op_when_store_missing():
	conn = _make_conn(with_store=False)
	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {"last_msg_id": "m1"}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)
	conn.websocket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_resume_last_msg_id_is_last_event_returns_empty():
	# Client is fully caught up; resume should send nothing.
	conn = _make_conn()
	for i in range(1, 4):
		await conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {}})
	conn.websocket.send_json.reset_mock()

	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {"last_msg_id": "m3"}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)
	conn.websocket.send_json.assert_not_called()


@pytest.mark.asyncio
async def test_resume_does_not_re_push_to_stream():
	# The replay sends go through websocket.send_json directly, not
	# conn.send(). If they went through conn.send they'd duplicate
	# every replay into the stream, which would cascade exponentially
	# on successive resumes.
	conn = _make_conn()
	for i in range(1, 4):
		await conn.send({"msg_id": f"m{i}", "type": "agent_status", "data": {}})
	count_before = len(await conn.store.get_events("t.site", "conv-1"))
	assert count_before == 3

	await _handle_custom_message(
		{"msg_id": "r1", "type": "resume", "data": {"last_msg_id": "m1"}},
		conn.websocket, conn, conversation_id=conn.conversation_id,
	)
	count_after = len(await conn.store.get_events("t.site", "conv-1"))
	# Stream size unchanged: replays re-sent over the wire but were NOT
	# re-persisted.
	assert count_after == count_before
