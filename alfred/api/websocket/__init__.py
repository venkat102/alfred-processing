"""WebSocket handler package (TD-H2 split from ``alfred/api/websocket.py``).

Protocol:
1. Client connects to /ws/{conversation_id}
2. Client sends handshake: {"api_key": "...", "jwt_token": "...", "site_config": {...}}
3. Server validates API key + JWT, extracts site_id and user
4. Bidirectional messaging begins - each message has a msg_id for ack tracking
5. MCP (JSON-RPC) messages are identified by "jsonrpc" field, all others by "type" field
6. Heartbeat ping every 30 seconds
7. On disconnect: the in-flight pipeline is cancelled so orphaned crews
   don't keep burning LLM calls. Durable queueing of inbound prompts lives
   on the client side (alfred_client's connection_manager persists to Redis
   and drains on reconnect); the processing app itself is stateless across
   disconnects.

Module layout (post-TD-H2 split):
  - ``extract``          — pure JSON / changeset / tool-activity helpers
  - ``connection``       — ConnectionState, handshake, message handlers,
                           the FastAPI websocket endpoint + heartbeat
  - ``pipeline_stages``  — dry-run retry, clarify, rescue

The public surface is unchanged — every name previously reachable via
``from alfred.api.websocket import X`` is re-exported from this module.
Tests that ``patch("alfred.api.websocket.X", ...)`` keep working
because the attributes still hang off ``alfred.api.websocket``.
"""

from __future__ import annotations

from alfred.api.websocket.connection import (  # noqa: E402
	_EXPIRED_Q_TTL_SECONDS,
	WS_CLOSE_AUTH_FAILED,
	WS_CLOSE_HEARTBEAT_TIMEOUT,
	WS_CLOSE_INVALID_HANDSHAKE,
	WS_CLOSE_RATE_LIMIT,
	ConnectionState,
	_authenticate_handshake,
	_classify_message,
	_connections,
	_handle_custom_message,
	_handle_mcp_message,
	_heartbeat_loop,
	_run_agent_pipeline,
	websocket_endpoint,
	ws_router,
)
from alfred.api.websocket.extract import (  # noqa: E402 (re-export block)
	_CHAT_TEMPLATE_LEAKAGE,
	_CODE_FENCE_LINE,
	_TOOL_ACTIVITY,
	_describe_tool_call,
	_extract_changes,
	_find_balanced_close,
	_parse_first_json_value,
	_validate_changeset_shape,
)
from alfred.api.websocket.pipeline_stages import (  # noqa: E402
	_clarify_requirements,
	_dry_run_with_retry,
	_rescue_regenerate_changeset,
)

# Tests + downstream packages import every re-exported name above via
# the package path (``alfred.api.websocket.X``); list them all so ruff
# F401 doesn't flag them. Leading-underscore names are still package-
# private by convention.
__all__ = [
	"ws_router",
	"ConnectionState",
	"WS_CLOSE_AUTH_FAILED",
	"WS_CLOSE_HEARTBEAT_TIMEOUT",
	"WS_CLOSE_INVALID_HANDSHAKE",
	"WS_CLOSE_RATE_LIMIT",
	"_CHAT_TEMPLATE_LEAKAGE",
	"_CODE_FENCE_LINE",
	"_EXPIRED_Q_TTL_SECONDS",
	"_TOOL_ACTIVITY",
	"_authenticate_handshake",
	"_classify_message",
	"_clarify_requirements",
	"_connections",
	"_describe_tool_call",
	"_dry_run_with_retry",
	"_extract_changes",
	"_find_balanced_close",
	"_handle_custom_message",
	"_handle_mcp_message",
	"_heartbeat_loop",
	"_parse_first_json_value",
	"_rescue_regenerate_changeset",
	"_run_agent_pipeline",
	"_validate_changeset_shape",
	"websocket_endpoint",
]
