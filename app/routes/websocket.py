"""
WebSocket routes — real-time deal stream per organization.

Endpoints
---------
WS   /ws/{org_id}              Real-time event stream (deals, approvals, tasks)
GET  /ws/stats                 Connection stats for monitoring
"""

import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.websocket_manager import manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/{org_id}")
async def websocket_endpoint(websocket: WebSocket, org_id: str):
    """
    WebSocket endpoint for real-time updates.

    Protocol:
      - Server sends events as JSON: { type, event_type, ... }
      - Server sends periodic pings: { type: "ping" }
      - Client should respond to pings with: { type: "pong" }
      - Client can send any text to keep connection alive
    """
    conn_info = await manager.connect(websocket, org_id)

    try:
        while True:
            raw = await websocket.receive_text()

            # Parse client messages for pong and other commands
            try:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")

                if msg_type == "pong":
                    manager.handle_pong(websocket, org_id)

                elif msg_type == "ping":
                    # Client-initiated ping — respond with pong
                    await websocket.send_text(json.dumps({
                        "type": "pong",
                        "echo": msg.get("id"),
                    }))

            except (json.JSONDecodeError, AttributeError):
                # Raw text keepalive — just acknowledge
                pass

    except WebSocketDisconnect:
        manager.disconnect(websocket, org_id)
    except Exception as e:
        logger.error("[WS] Unexpected error for org=%s: %s", org_id, e)
        manager.disconnect(websocket, org_id)


@router.get("/ws/stats", tags=["websocket"])
def websocket_stats():
    """Return current WebSocket connection statistics."""
    return manager.get_stats()
