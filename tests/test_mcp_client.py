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
