"""MCP Client for the Processing App.

Sends JSON-RPC 2.0 requests through the WebSocket connection to the
Client App's MCP server, and correlates responses by request ID.

Supports concurrent requests with asyncio futures for request-response matching.
"""

import asyncio
import json
import logging
import uuid
from concurrent.futures import TimeoutError as concurrent_futures_TimeoutError
from typing import Any

logger = logging.getLogger("alfred.mcp_client")

# Default timeout for MCP tool calls
DEFAULT_TIMEOUT = 30  # seconds


class MCPClient:
	"""MCP client that communicates with the Client App's MCP server over WebSocket.

	Manages request-response correlation via JSON-RPC id field and async futures.

	Thread safety: all futures are bound to a single `main_loop` (the asyncio loop
	that owns the WebSocket). `handle_response` can be called from any thread - it
	uses `call_soon_threadsafe` to schedule the future resolution on the main loop.
	`call_sync` can be called from a worker thread (e.g. a CrewAI tool wrapper) and
	safely drives `call_tool` on the main loop via `run_coroutine_threadsafe`.
	"""

	def __init__(
		self,
		send_func,
		main_loop: asyncio.AbstractEventLoop | None = None,
		timeout: int = DEFAULT_TIMEOUT,
		on_call=None,
	):
		"""Initialize the MCP client.

		Args:
			send_func: Async callable that sends a message over the WebSocket.
				Signature: async def send(message: dict) -> None. Must be invoked
				on the main loop.
			main_loop: The asyncio event loop that owns the WebSocket. Futures
				and response resolution happen on this loop. If None, the loop
				is captured lazily on the first `call_tool` / `call_sync` invocation,
				so the client can be constructed from a sync context (e.g. pytest
				fixtures) as long as it's used from an async context later.
				Production code in `_authenticate_handshake` passes this
				explicitly - capturing at handshake time is more reliable than
				relying on whichever loop happens to be running at first use.
			timeout: Default timeout in seconds for tool calls.
			on_call: Optional async callback invoked before each tool call.
				Signature: async def on_call(tool_name: str, arguments: dict) -> None.
				Used by the pipeline to stream activity updates to the UI.
				Exceptions in the callback are logged but don't affect the tool call.
		"""
		self._send = send_func
		self._main_loop = main_loop  # May be None; captured lazily
		self._timeout = timeout
		self._on_call = on_call
		self._pending: dict[str, asyncio.Future] = {}

	def _ensure_main_loop(self) -> asyncio.AbstractEventLoop:
		"""Return self._main_loop, capturing the running loop on first call if unset.

		Must be called from an async context on first use. Subsequent calls
		return the cached loop regardless of calling context.
		"""
		if self._main_loop is None:
			try:
				self._main_loop = asyncio.get_running_loop()
			except RuntimeError as e:
				raise RuntimeError(
					"MCPClient has no main_loop and is not inside a running loop. "
					"Pass main_loop=asyncio.get_running_loop() at construction, or "
					"ensure the first call_tool/call_sync runs in an async context."
				) from e
		return self._main_loop

	@property
	def main_loop(self) -> asyncio.AbstractEventLoop | None:
		return self._main_loop

	async def call_tool(self, tool_name: str, arguments: dict | None = None) -> dict:
		"""Call an MCP tool on the Client App.

		Must be awaited on the main loop. From a worker thread, use `call_sync`.

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
		loop = self._ensure_main_loop()
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

		future = loop.create_future()
		self._pending[request_id] = future

		# Notify observers (e.g. UI activity stream) BEFORE sending. Errors here
		# must not break the MCP call - this is purely a side channel.
		if self._on_call is not None:
			try:
				await self._on_call(tool_name, arguments or {})
			except Exception as e:
				logger.debug("MCP on_call callback failed for %s: %s", tool_name, e)

		try:
			await self._send(request)
			logger.debug("MCP request sent: tool=%s, id=%s", tool_name, request_id)

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

	def call_sync(
		self,
		tool_name: str,
		arguments: dict | None = None,
		timeout: int | None = None,
	) -> dict:
		"""Call an MCP tool from a worker thread and block until the result arrives.

		This is the primitive CrewAI @tool wrappers use. It schedules `call_tool`
		on the main loop via `run_coroutine_threadsafe` and waits for the result
		in the calling thread. Safe to call from any thread that isn't the main
		loop itself.

		Args:
			tool_name: Name of the tool to call.
			arguments: Tool arguments dict.
			timeout: Per-call timeout override (seconds). Defaults to client's timeout.

		Returns:
			Tool result dict.

		Raises:
			TimeoutError: If the result doesn't arrive within timeout.
			RuntimeError: If the MCP server returns an error.
		"""
		if self._main_loop is None:
			raise RuntimeError(
				"MCPClient.call_sync requires main_loop to be set. Either pass "
				"main_loop explicitly to MCPClient() or await call_tool() at least "
				"once from the main loop before calling call_sync from a worker thread."
			)

		# Deadlock guard: if someone calls call_sync from the main loop thread
		# (e.g. a refactor that awaits a CrewAI tool directly), future.result()
		# would block the loop forever. Detect and fail fast.
		try:
			current_loop = asyncio.get_running_loop()
		except RuntimeError:
			current_loop = None
		if current_loop is self._main_loop:
			raise RuntimeError(
				"MCPClient.call_sync called from the main event loop thread - "
				"this would deadlock. Use `await call_tool(...)` instead."
			)

		effective_timeout = timeout if timeout is not None else self._timeout
		coro = self.call_tool(tool_name, arguments)
		future = asyncio.run_coroutine_threadsafe(coro, self._main_loop)
		try:
			return future.result(timeout=effective_timeout + 5)  # slight buffer over the await
		except concurrent_futures_TimeoutError:
			future.cancel()
			raise TimeoutError(f"MCP tool '{tool_name}' timed out after {effective_timeout}s")

	async def list_tools(self) -> list[dict]:
		"""Get the list of available MCP tools from the Client App."""
		loop = self._ensure_main_loop()
		request_id = str(uuid.uuid4())
		request = {
			"jsonrpc": "2.0",
			"method": "tools/list",
			"id": request_id,
		}

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
		Schedules the future resolution on the main loop via `call_soon_threadsafe`
		so it's safe to call from any thread (though in practice it runs on the
		main loop where the WS listener lives).

		If `main_loop` isn't set yet (no async call has happened), we fall back
		to resolving the future directly - this handles the test fixture case
		where handle_response is invoked from the same loop that created the
		future via call_tool.
		"""
		request_id = message.get("id")
		if request_id is None:
			logger.warning("Received MCP response without id: %s", message)
			return

		future = self._pending.get(str(request_id))
		if future is None:
			logger.warning("Received MCP response for unknown id: %s", request_id)
			return

		# Guard against double-resolution from a buggy server (same id twice)
		# or a late response arriving after a timeout already resolved the future.
		if future.done():
			logger.debug("Ignoring late MCP response for already-resolved id: %s", request_id)
			return

		def _safe_set_result(fut: asyncio.Future, value):
			if not fut.done():
				fut.set_result(value)

		def _safe_set_exception(fut: asyncio.Future, exc: BaseException):
			if not fut.done():
				fut.set_exception(exc)

		# If main_loop is set, use call_soon_threadsafe for cross-thread safety.
		# If not (single-loop test path), resolve directly on the future's loop.
		main_loop = self._main_loop
		if main_loop is None:
			schedule = lambda fn, *args: fn(*args)  # noqa: E731
		else:
			schedule = lambda fn, *args: main_loop.call_soon_threadsafe(fn, *args)  # noqa: E731

		if "error" in message:
			error = message["error"]
			exc = RuntimeError(f"MCP error [{error.get('code')}]: {error.get('message')}")
			schedule(_safe_set_exception, future, exc)
			return

		if "result" not in message:
			schedule(
				_safe_set_exception, future,
				RuntimeError(f"Malformed MCP response: {message}"),
			)
			return

		# Extract the actual tool result from MCP content format.
		# {"content": [{"type": "text", "text": "..."}]}
		result = message["result"]
		if isinstance(result, dict) and "content" in result:
			content = result["content"]
			if content and isinstance(content, list):
				text = content[0].get("text", "{}")
				try:
					payload = json.loads(text)
				except json.JSONDecodeError:
					payload = {"raw": text}
				schedule(_safe_set_result, future, payload)
				return

		schedule(_safe_set_result, future, result)

	@property
	def pending_count(self) -> int:
		"""Number of pending requests waiting for responses."""
		return len(self._pending)
