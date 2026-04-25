"""Regression test for the audit's P1.4 — dead ``pending_acks`` dict.

The dict was initialised in ``ConnectionState.__init__`` and copied
onto ``_RestConn.__init__``, but nothing on the server ever
populated it — the pop on ack receipt was always a no-op. The wire
was half-built and stuck that way since the master merge.

Removing the dict cleared the "looks wired, isn't" smell. The ack
handler's other side effect (``last_acked_msg_id``) is still live
and used by the ``resume`` replay handler to know where to start
from. These tests pin both: the field is gone AND the live
behaviour around it is preserved.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.api.rest_runner import _RestConn
from alfred.api.websocket import ConnectionState, _handle_custom_message


def _make_conn() -> ConnectionState:
	ws = MagicMock()
	ws.send_json = AsyncMock()
	ws.app = MagicMock()
	ws.app.state = MagicMock()
	ws.app.state.shutting_down = False
	return ConnectionState(
		websocket=ws, site_id="site-a", user="u@x",
		roles=["System Manager"], site_config={},
		conversation_id="conv-1",
	)


def test_connection_state_no_longer_carries_pending_acks():
	conn = _make_conn()
	# Field is gone — getattr to avoid an AttributeError tripping
	# the assertion message itself.
	assert not hasattr(conn, "pending_acks")


def test_rest_conn_no_longer_carries_pending_acks():
	conn = _RestConn(
		site_id="site-a", user="u@x", roles=[],
		site_config={}, store=MagicMock(), task_id="t-x",
		redis=MagicMock(), settings=MagicMock(),
	)
	assert not hasattr(conn, "pending_acks")


@pytest.mark.asyncio
async def test_ack_still_updates_last_acked_msg_id():
	"""The live half of the ack handler — ``last_acked_msg_id`` is
	still used by ``resume`` to compute the replay start point."""
	conn = _make_conn()
	assert conn.last_acked_msg_id is None

	await _handle_custom_message(
		data={
			"msg_id": "outer",
			"type": "ack",
			"data": {"msg_id": "m-42"},
		},
		websocket=conn.websocket, conn=conn, conversation_id="conv-1",
	)

	# The acked id flows in via ``data.msg_id``; outer envelope id is
	# ignored when an explicit one is present.
	assert conn.last_acked_msg_id == "m-42"


@pytest.mark.asyncio
async def test_ack_falls_back_to_outer_msg_id_when_data_missing():
	"""Older clients sent the acked id at the envelope level rather
	than nested under ``data``. The fallback still works."""
	conn = _make_conn()

	await _handle_custom_message(
		data={"msg_id": "legacy-id", "type": "ack", "data": {}},
		websocket=conn.websocket, conn=conn, conversation_id="conv-1",
	)

	assert conn.last_acked_msg_id == "legacy-id"
