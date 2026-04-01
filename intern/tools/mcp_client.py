"""MCP Client for the Processing App.

Sends JSON-RPC 2.0 requests through the WebSocket connection to the
Client App's MCP server, and correlates responses by request ID.

Supports concurrent requests with asyncio futures for request-response matching.
"""

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger("alfred.mcp_client")

# Default timeout for MCP tool calls
DEFAULT_TIMEOUT = 30  # seconds


class MCPClient:
	"""MCP client that communicates with the Client App's MCP server over WebSocket.

	Manages request-response correlation via JSON-RPC id field and async futures.
	"""

	def __init__(self, send_func, timeout: int = DEFAULT_TIMEOUT):
		"""Initialize the MCP client.

		Args:
			send_func: Async callable that sends a message over the WebSocket.
				Signature: async def send(message: dict) -> None
			timeout: Default timeout in seconds for tool calls.
		"""
		self._send = send_func
		self._timeout = timeout
		self._pending: dict[str, asyncio.Future] = {}

	async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
		"""Call an MCP tool on the Client App.

		Args:
			tool_name: Name of the tool to call.
			arguments: Tool arguments dict.

		Returns:
			Tool result dict.

		Raises:
			TimeoutError: If the Client App doesn't respond within timeout.
			ConnectionError: If the WebSocket is disconnected.
			RuntimeError: If the MCP server returns an error.
		"""
		request_id = str(uuid.uuid4())
		request = {
			"jsonrpc": "2.0",
			"method": "tools/call",
			"params": {
				"name": tool_name,
				"arguments": arguments or {},
			},
			"id": request_id,
		}

		# Create a future for the response
		loop = asyncio.get_event_loop()
		future = loop.create_future()
		self._pending[request_id] = future

		try:
			await self._send(request)
			logger.debug("MCP request sent: tool=%s, id=%s", tool_name, request_id)

			# Wait for the response with timeout
			result = await asyncio.wait_for(future, timeout=self._timeout)
			return result
		except asyncio.TimeoutError:
			logger.warning("MCP timeout: tool=%s, id=%s (after %ds)", tool_name, request_id, self._timeout)
			raise TimeoutError(f"MCP tool '{tool_name}' timed out after {self._timeout}s")
		except Exception as e:
			logger.error("MCP call failed: tool=%s, error=%s", tool_name, e)
			raise
		finally:
			self._pending.pop(request_id, None)

	async def list_tools(self) -> list[dict]:
		"""Get the list of available MCP tools from the Client App."""
		request_id = str(uuid.uuid4())
		request = {
			"jsonrpc": "2.0",
			"method": "tools/list",
			"id": request_id,
		}

		loop = asyncio.get_event_loop()
		future = loop.create_future()
		self._pending[request_id] = future

		try:
			await self._send(request)
			result = await asyncio.wait_for(future, timeout=self._timeout)
			return result.get("tools", [])
		except asyncio.TimeoutError:
			raise TimeoutError("MCP tools/list timed out")
		finally:
			self._pending.pop(request_id, None)

	def handle_response(self, message: dict):
		"""Handle an incoming JSON-RPC response from the Client App.

		Called by the WebSocket message router when an MCP response is received.
		Resolves the corresponding pending future.
		"""
		request_id = message.get("id")
		if request_id is None:
			logger.warning("Received MCP response without id: %s", message)
			return

		future = self._pending.get(str(request_id))
		if future is None:
			logger.warning("Received MCP response for unknown id: %s", request_id)
			return

		if "error" in message:
			error = message["error"]
			future.set_exception(
				RuntimeError(f"MCP error [{error.get('code')}]: {error.get('message')}")
			)
		elif "result" in message:
			# Extract the actual tool result from MCP content format
			result = message["result"]
			if isinstance(result, dict) and "content" in result:
				# MCP response format: {"content": [{"type": "text", "text": "..."}]}
				content = result["content"]
				if content and isinstance(content, list):
					text = content[0].get("text", "{}")
					try:
						future.set_result(json.loads(text))
					except json.JSONDecodeError:
						future.set_result({"raw": text})
				else:
					future.set_result(result)
			else:
				future.set_result(result)
		else:
			future.set_exception(RuntimeError(f"Malformed MCP response: {message}"))

	@property
	def pending_count(self) -> int:
		"""Number of pending requests waiting for responses."""
		return len(self._pending)
