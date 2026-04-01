"""User interaction tool for agent-to-user communication.

The ask_user tool sends questions to the user via the custom WebSocket channel
(NOT the MCP channel) and blocks until the user responds or timeout is reached.
"""

import asyncio
import json
import logging
import uuid

logger = logging.getLogger("alfred.user_interaction")

# Default timeout for user responses
DEFAULT_USER_TIMEOUT = 900  # 15 minutes


class UserInteractionHandler:
	"""Manages agent-to-user question-response interactions via WebSocket.

	Sends questions to the user through the custom WebSocket message channel
	and waits for their response using async futures.
	"""

	def __init__(self, send_func, timeout: int = DEFAULT_USER_TIMEOUT):
		"""Initialize the handler.

		Args:
			send_func: Async callable that sends a custom message over WebSocket.
			timeout: Max wait time for user response in seconds.
		"""
		self._send = send_func
		self._timeout = timeout
		self._pending: dict[str, asyncio.Future] = {}

	async def ask_user(self, question: str, choices: list[str] | None = None) -> str:
		"""Send a question to the user and wait for their response.

		Args:
			question: The question text.
			choices: Optional list of answer choices.

		Returns:
			The user's response string.

		Raises:
			TimeoutError: If user doesn't respond within timeout.
		"""
		msg_id = str(uuid.uuid4())
		message = {
			"msg_id": msg_id,
			"type": "question",
			"data": {
				"question": question,
				"choices": choices or [],
				"timeout_seconds": self._timeout,
			},
		}

		loop = asyncio.get_event_loop()
		future = loop.create_future()
		self._pending[msg_id] = future

		try:
			await self._send(message)
			logger.info("Question sent to user: %s (id=%s)", question[:80], msg_id)

			response = await asyncio.wait_for(future, timeout=self._timeout)
			logger.info("User responded to %s: %s", msg_id, response[:80] if response else "")
			return response
		except asyncio.TimeoutError:
			logger.warning("User response timeout for question %s (after %ds)", msg_id, self._timeout)
			raise TimeoutError(
				f"User did not respond within {self._timeout} seconds. "
				"The conversation will be suspended."
			)
		finally:
			self._pending.pop(msg_id, None)

	def handle_user_response(self, message: dict):
		"""Handle an incoming user response message.

		Called by the WebSocket message router when a user_response message arrives.

		Expected message format:
			{"msg_id": "...", "type": "user_response", "data": {"text": "user's answer"}}
		"""
		msg_id = message.get("msg_id", "")
		data = message.get("data", {})

		# Check both the message's own msg_id and any referenced msg_id in data
		response_text = data.get("text", "")
		ref_msg_id = data.get("response_to", msg_id)

		future = self._pending.get(ref_msg_id) or self._pending.get(msg_id)
		if future is None:
			logger.warning("Received user response for unknown question: %s", msg_id)
			return

		if not future.done():
			future.set_result(response_text)

	@property
	def pending_count(self) -> int:
		"""Number of questions waiting for user responses."""
		return len(self._pending)


def build_ask_user_tool(handler: UserInteractionHandler):
	"""Create a CrewAI @tool that uses the UserInteractionHandler.

	Args:
		handler: An initialized UserInteractionHandler.

	Returns:
		A CrewAI tool function.
	"""
	from crewai.tools import tool

	@tool
	def ask_user(question: str, choices: str = "") -> str:
		"""Ask the user a question and wait for their response. Use for clarifying requirements or getting approval. Optionally provide comma-separated choices."""
		choice_list = [c.strip() for c in choices.split(",") if c.strip()] if choices else None

		try:
			loop = asyncio.get_event_loop()
			if loop.is_running():
				import concurrent.futures
				with concurrent.futures.ThreadPoolExecutor() as pool:
					future = pool.submit(asyncio.run, handler.ask_user(question, choice_list))
					return future.result(timeout=handler._timeout + 5)
			else:
				return loop.run_until_complete(handler.ask_user(question, choice_list))
		except RuntimeError:
			return asyncio.run(handler.ask_user(question, choice_list))
		except TimeoutError:
			return "[TIMEOUT] User did not respond. Consider escalating to a human operator."

	return ask_user
