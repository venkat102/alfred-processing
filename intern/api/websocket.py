"""WebSocket handler for real-time bidirectional communication with client apps."""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("alfred.websocket")

ws_router = APIRouter()


@ws_router.websocket("/ws/{conversation_id}")
async def websocket_endpoint(websocket: WebSocket, conversation_id: str):
	"""WebSocket endpoint for client app communication.

	Accepts connections at /ws/{conversation_id}. Authentication and message
	routing will be implemented in Task 1.5 (API Gateway).
	"""
	await websocket.accept()
	logger.info("WebSocket connected: conversation=%s", conversation_id)

	try:
		while True:
			data = await websocket.receive_text()
			# Echo back for now — full message routing in Task 1.5
			await websocket.send_json({
				"type": "echo",
				"conversation_id": conversation_id,
				"data": data,
			})
	except WebSocketDisconnect:
		logger.info("WebSocket disconnected: conversation=%s", conversation_id)
