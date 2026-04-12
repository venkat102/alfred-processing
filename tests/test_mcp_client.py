"""Tests for MCP client, CrewAI tool wrappers, and user interaction."""

import asyncio
import json

import pytest

from alfred.tools.mcp_client import MCPClient
from alfred.tools.user_interaction import UserInteractionHandler


# ── Mock WebSocket ────────────────────────────────────────────────

class MockWebSocket:
	"""Mock WebSocket that simulates MCP server responses."""

	def __init__(self):
		self.sent_messages = []
		self.auto_respond = True

	async def send(self, message: dict):
		self.sent_messages.append(message)
		# Auto-respond to simulate the MCP server
		if self.auto_respond and "id" in message:
			return message

	def make_response(self, request_id: str, result: dict) -> dict:
		"""Build a mock MCP response."""
		return {
			"jsonrpc": "2.0",
			"id": request_id,
			"result": {
				"content": [{"type": "text", "text": json.dumps(result)}],
			},
		}


# ── MCP Client Tests ─────────────────────────────────────────────

class TestMCPClient:
	@pytest.fixture
	def mock_ws(self):
		return MockWebSocket()

	@pytest.fixture
	def client(self, mock_ws):
		return MCPClient(mock_ws.send, timeout=5)

	async def test_call_tool_sends_request(self, client, mock_ws):
		# Start the call (it will timeout since there's no real response)
		mock_ws.auto_respond = False

		async def delayed_response():
			await asyncio.sleep(0.1)
			# Find the request and respond
			if mock_ws.sent_messages:
				req = mock_ws.sent_messages[-1]
				client.handle_response(mock_ws.make_response(
					req["id"], {"frappe_version": "17.0.0"}
				))

		task = asyncio.create_task(delayed_response())
		result = await client.call_tool("get_site_info")
		await task

		assert result["frappe_version"] == "17.0.0"
		assert len(mock_ws.sent_messages) == 1
		assert mock_ws.sent_messages[0]["method"] == "tools/call"
		assert mock_ws.sent_messages[0]["params"]["name"] == "get_site_info"

	async def test_call_tool_with_arguments(self, client, mock_ws):
		mock_ws.auto_respond = False

		async def respond():
			await asyncio.sleep(0.1)
			req = mock_ws.sent_messages[-1]
			client.handle_response(mock_ws.make_response(
				req["id"], {"doctype": "User", "fields": []}
			))

		task = asyncio.create_task(respond())
		result = await client.call_tool("get_doctype_schema", {"doctype": "User"})
		await task

		assert result["doctype"] == "User"
		assert mock_ws.sent_messages[0]["params"]["arguments"]["doctype"] == "User"

	async def test_call_tool_timeout(self, client, mock_ws):
		mock_ws.auto_respond = False
		client._timeout = 0.5  # Short timeout for test

		with pytest.raises(TimeoutError, match="timed out"):
			await client.call_tool("get_site_info")

	async def test_handle_error_response(self, client, mock_ws):
		mock_ws.auto_respond = False

		async def respond_with_error():
			await asyncio.sleep(0.1)
			req = mock_ws.sent_messages[-1]
			client.handle_response({
				"jsonrpc": "2.0",
				"id": req["id"],
				"error": {"code": -32601, "message": "Tool not found"},
			})

		task = asyncio.create_task(respond_with_error())
		with pytest.raises(RuntimeError, match="Tool not found"):
			await client.call_tool("nonexistent_tool")
		await task

	async def test_concurrent_requests(self, client, mock_ws):
		"""Two concurrent calls should be correlated correctly."""
		mock_ws.auto_respond = False

		async def respond_both():
			await asyncio.sleep(0.1)
			# Respond to requests in reverse order
			for req in reversed(mock_ws.sent_messages):
				tool_name = req["params"]["name"]
				client.handle_response(mock_ws.make_response(
					req["id"], {"tool": tool_name}
				))

		task = asyncio.create_task(respond_both())
		results = await asyncio.gather(
			client.call_tool("get_site_info"),
			client.call_tool("get_doctypes"),
		)
		await task

		tool_names = {r["tool"] for r in results}
		assert tool_names == {"get_site_info", "get_doctypes"}

	async def test_list_tools(self, client, mock_ws):
		mock_ws.auto_respond = False

		async def respond():
			await asyncio.sleep(0.1)
			req = mock_ws.sent_messages[-1]
			client.handle_response({
				"jsonrpc": "2.0",
				"id": req["id"],
				"result": {"tools": [{"name": "get_site_info", "description": "..."}]},
			})

		task = asyncio.create_task(respond())
		tools = await client.list_tools()
		await task

		assert len(tools) == 1
		assert tools[0]["name"] == "get_site_info"

	async def test_pending_count(self, client, mock_ws):
		assert client.pending_count == 0

	async def test_on_call_callback_fires_before_send(self, client, mock_ws):
		"""The on_call hook must fire with tool name + arguments BEFORE the
		JSON-RPC request is sent, so UI activity streams accurately show
		what's about to happen."""
		calls = []

		async def on_call(tool_name, arguments):
			calls.append((tool_name, dict(arguments)))

		client._on_call = on_call
		mock_ws.auto_respond = False

		async def respond():
			await asyncio.sleep(0.05)
			req = mock_ws.sent_messages[-1]
			client.handle_response(mock_ws.make_response(req["id"], {"ok": True}))

		task = asyncio.create_task(respond())
		await client.call_tool("get_doctype_schema", {"doctype": "Leave Application"})
		await task

		assert calls == [("get_doctype_schema", {"doctype": "Leave Application"})]

	async def test_on_call_callback_exception_does_not_break_call(self, client, mock_ws):
		"""A buggy on_call callback must not affect the tool call itself."""
		async def broken_on_call(tool_name, arguments):
			raise RuntimeError("boom")

		client._on_call = broken_on_call
		mock_ws.auto_respond = False

		async def respond():
			await asyncio.sleep(0.05)
			req = mock_ws.sent_messages[-1]
			client.handle_response(mock_ws.make_response(req["id"], {"ok": True}))

		task = asyncio.create_task(respond())
		result = await client.call_tool("get_site_info")
		await task

		# Callback failed but tool call still succeeded
		assert result == {"ok": True}

	async def test_handle_response_ignores_unknown_id(self, client, mock_ws):
		"""Late or bogus responses (no pending future) must not crash."""
		client.handle_response({
			"jsonrpc": "2.0",
			"id": "not-a-real-request",
			"result": {"content": [{"type": "text", "text": "{}"}]},
		})
		# No exception raised = pass

	async def test_handle_response_double_resolution_safe(self, client, mock_ws):
		"""If the server replies twice for the same id (buggy MCP server), the
		second response must be silently ignored instead of raising InvalidStateError."""
		mock_ws.auto_respond = False

		async def respond_twice():
			await asyncio.sleep(0.05)
			req = mock_ws.sent_messages[-1]
			client.handle_response(mock_ws.make_response(req["id"], {"first": True}))
			# Second response for same id - should be ignored cleanly
			client.handle_response(mock_ws.make_response(req["id"], {"second": True}))

		task = asyncio.create_task(respond_twice())
		result = await client.call_tool("get_site_info")
		await task

		# First response wins
		assert result == {"first": True}

	async def test_handle_response_missing_id(self, client, mock_ws):
		"""Responses without id must not crash the handler."""
		client.handle_response({"jsonrpc": "2.0", "result": {"content": []}})
		# No exception raised = pass

	async def test_call_sync_from_worker_thread(self, client, mock_ws):
		"""call_sync must work when invoked from a non-main-loop thread (the
		CrewAI tool dispatch path). This is the core cross-thread scenario
		that the ThreadPoolExecutor-based _run_async previously got wrong."""
		import threading

		# Capture the main loop so handle_response runs cleanly via call_soon_threadsafe
		client._main_loop = asyncio.get_running_loop()
		mock_ws.auto_respond = False

		# Autoresponder task that resolves pending requests from the main loop
		async def autoresponder():
			for _ in range(20):
				await asyncio.sleep(0.02)
				if mock_ws.sent_messages:
					req = mock_ws.sent_messages[-1]
					client.handle_response(mock_ws.make_response(
						req["id"], {"from_thread": True}
					))
					return

		task = asyncio.create_task(autoresponder())

		# Call from a worker thread via run_in_executor
		loop = asyncio.get_running_loop()
		def sync_caller():
			return client.call_sync("get_site_info", {})

		result = await loop.run_in_executor(None, sync_caller)
		await task

		assert result == {"from_thread": True}

	async def test_call_sync_deadlock_guard(self, client, mock_ws):
		"""Calling call_sync from the main loop thread must raise a clear
		RuntimeError rather than deadlocking forever on future.result()."""
		client._main_loop = asyncio.get_running_loop()

		with pytest.raises(RuntimeError, match="main event loop thread"):
			client.call_sync("get_site_info", {})

	async def test_call_sync_requires_main_loop(self, mock_ws):
		"""call_sync without a main_loop set is unusable - it has no loop to
		dispatch to. Must fail with a clear error message."""
		client = MCPClient(mock_ws.send, timeout=1)
		# _main_loop is None (lazy-init only on async call)

		import threading
		errors = []
		def worker():
			try:
				client.call_sync("get_site_info", {})
			except RuntimeError as e:
				errors.append(str(e))

		t = threading.Thread(target=worker)
		t.start()
		t.join(timeout=2)

		assert len(errors) == 1
		assert "main_loop" in errors[0]


# ── User Interaction Tests ────────────────────────────────────────

class TestUserInteraction:
	@pytest.fixture
	def mock_ws(self):
		return MockWebSocket()

	@pytest.fixture
	def handler(self, mock_ws):
		return UserInteractionHandler(mock_ws.send, timeout=5)

	async def test_ask_user_sends_question(self, handler, mock_ws):
		async def respond():
			await asyncio.sleep(0.1)
			msg = mock_ws.sent_messages[-1]
			handler.handle_user_response({
				"msg_id": msg["msg_id"],
				"type": "user_response",
				"data": {"text": "Yes, add a workflow", "response_to": msg["msg_id"]},
			})

		task = asyncio.create_task(respond())
		answer = await handler.ask_user("Do you want a workflow?", ["Yes", "No"])
		await task

		assert answer == "Yes, add a workflow"
		assert mock_ws.sent_messages[0]["type"] == "question"
		assert "Do you want a workflow?" in mock_ws.sent_messages[0]["data"]["question"]

	async def test_ask_user_with_choices(self, handler, mock_ws):
		async def respond():
			await asyncio.sleep(0.1)
			msg = mock_ws.sent_messages[-1]
			handler.handle_user_response({
				"msg_id": msg["msg_id"],
				"type": "user_response",
				"data": {"text": "Option A", "response_to": msg["msg_id"]},
			})

		task = asyncio.create_task(respond())
		answer = await handler.ask_user("Pick one:", ["Option A", "Option B"])
		await task

		assert answer == "Option A"
		assert mock_ws.sent_messages[0]["data"]["choices"] == ["Option A", "Option B"]

	async def test_ask_user_timeout(self, handler):
		handler._timeout = 0.5
		with pytest.raises(TimeoutError, match="did not respond"):
			await handler.ask_user("This will timeout")

	async def test_pending_count(self, handler):
		assert handler.pending_count == 0
