"""Pydantic models for API request/response schemas and message types."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Message Type Constants ────────────────────────────────────────

class MessageType(str, Enum):
	"""All custom message types supported by the WebSocket protocol."""
	PROMPT = "prompt"
	USER_RESPONSE = "user_response"
	DEPLOY_COMMAND = "deploy_command"
	AGENT_STATUS = "agent_status"
	QUESTION = "question"
	PREVIEW = "preview"
	CHANGESET = "changeset"
	ERROR = "error"
	ACK = "ack"
	RESUME = "resume"
	ECHO = "echo"


# ── REST API Models ──────────────────────────────────────────────

class TaskCreateRequest(BaseModel):
	"""Request body for POST /api/v1/tasks."""
	prompt: str = Field(..., description="The user's request/instruction")
	user_context: dict[str, Any] = Field(
		default_factory=dict,
		description="User context: email, roles, permissions",
	)
	site_config: dict[str, Any] = Field(
		default_factory=dict,
		description="Site configuration: LLM provider, model, limits",
	)


class TaskCreateResponse(BaseModel):
	"""Response for POST /api/v1/tasks."""
	task_id: str
	status: str = "queued"


class TaskStatusResponse(BaseModel):
	"""Response for GET /api/v1/tasks/{task_id}."""
	task_id: str
	status: str
	current_agent: str | None = None
	data: dict[str, Any] = Field(default_factory=dict)


class TaskMessageResponse(BaseModel):
	"""Single message in the task message history."""
	id: str
	data: dict[str, Any]


class ErrorResponse(BaseModel):
	"""Standard error response."""
	error: str
	code: str


# ── WebSocket Protocol Models ────────────────────────────────────

class WSHandshakePayload(BaseModel):
	"""Payload sent during WebSocket handshake."""
	api_key: str
	jwt_token: str
	site_config: dict[str, Any] = Field(default_factory=dict)


class WSMessage(BaseModel):
	"""Generic WebSocket message envelope."""
	msg_id: str = Field(..., description="Unique message ID for acknowledgment tracking")
	type: str = Field(..., description="Message type (from MessageType enum or 'jsonrpc' for MCP)")
	data: dict[str, Any] = Field(default_factory=dict)


class WSAck(BaseModel):
	"""Acknowledgment message."""
	msg_id: str
	type: str = "ack"
