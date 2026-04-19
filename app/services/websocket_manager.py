"""
WebSocket Connection Manager — keyed by organization_id.

Manages per-org connection pools with:
  - Heartbeat ping/pong keepalive
  - Connection metadata tracking (connect time, last ping)
  - Typed broadcast helpers for deals, approvals, tasks
  - Graceful error handling and dead connection cleanup
  - Connection stats for monitoring
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Connection metadata
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConnectionInfo:
    """Tracks metadata for a single WebSocket connection."""
    websocket: WebSocket
    org_id: str
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_pong: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_count: int = 0


# ─────────────────────────────────────────────────────────────────────────────
# Connection Manager
# ─────────────────────────────────────────────────────────────────────────────

class ConnectionManager:
    """
    Manages WebSocket connections grouped by organization.

    Features:
      - Per-org isolation (multi-tenant)
      - Heartbeat ping/pong to detect stale connections
      - Typed broadcast methods for deals, approvals, and tasks
      - Dead connection cleanup
      - Connection stats endpoint data
    """

    # Heartbeat interval (seconds)
    HEARTBEAT_INTERVAL = 30
    # Max time since last pong before considering connection dead
    PONG_TIMEOUT = 90

    def __init__(self):
        # org_id → {websocket: ConnectionInfo}
        self._connections: dict[str, dict[WebSocket, ConnectionInfo]] = {}
        self._heartbeat_task: asyncio.Task | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def connect(self, websocket: WebSocket, org_id: str) -> ConnectionInfo:
        """Accept and register a new WS connection. Returns connection info."""
        await websocket.accept()

        info = ConnectionInfo(websocket=websocket, org_id=org_id)

        if org_id not in self._connections:
            self._connections[org_id] = {}
        self._connections[org_id][websocket] = info

        # Start heartbeat loop if not already running
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(
            "[WS] Connected: org=%s total_org=%d total_global=%d",
            org_id,
            len(self._connections.get(org_id, {})),
            self.total_connections,
        )

        # Send welcome message
        await self._send_safe(websocket, {
            "type": "connected",
            "org_id": org_id,
            "message": "WebSocket connected to AEGIBIT Flow",
            "server_time": datetime.now(timezone.utc).isoformat(),
        })

        return info

    def disconnect(self, websocket: WebSocket, org_id: str):
        """Remove a WS connection from the pool."""
        if org_id in self._connections:
            self._connections[org_id].pop(websocket, None)
            if not self._connections[org_id]:
                del self._connections[org_id]

        logger.info(
            "[WS] Disconnected: org=%s remaining=%d",
            org_id,
            len(self._connections.get(org_id, {})),
        )

    # ── Broadcasting ─────────────────────────────────────────────────────────

    async def broadcast(self, org_id: str, event: dict[str, Any]):
        """Push an event to all connected clients for a given org."""
        if org_id not in self._connections:
            return

        payload = json.dumps(event, default=str)
        dead: list[WebSocket] = []

        for ws, info in self._connections[org_id].items():
            try:
                await ws.send_text(payload)
                info.message_count += 1
            except Exception:
                dead.append(ws)

        # Cleanup stale connections
        for ws in dead:
            self._connections[org_id].pop(ws, None)
            logger.debug("[WS] Removed dead connection during broadcast for org=%s", org_id)

        if dead and not self._connections.get(org_id):
            del self._connections[org_id]

    async def broadcast_deal_event(
        self,
        org_id: str,
        event_type: str,
        deal: dict[str, Any],
        *,
        notification_id: str | None = None,
        title: str = "",
        message: str = "",
        severity: str = "INFO",
    ):
        """
        Broadcast a deal-related event with a standardized shape.

        Used for: deal_created, deal_approved, deal_rejected, status_changed
        """
        ws_type_map = {
            "DEAL_CREATED": "deal_created",
            "DEAL_AUTO_APPROVED": "deal_approved",
            "GM_APPROVED": "deal_approved",
            "GM_ESCALATED": "status_changed",
            "DIRECTOR_APPROVED": "deal_approved",
            "DEAL_REJECTED": "deal_rejected",
        }
        ws_type = ws_type_map.get(event_type, event_type.lower())

        await self.broadcast(org_id, {
            "type": ws_type,
            "event_type": event_type,
            "notification_id": notification_id,
            "title": title,
            "message": message,
            "severity": severity,
            "deal": deal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def broadcast_task_event(
        self,
        org_id: str,
        event_type: str,
        task_data: dict[str, Any],
    ):
        """
        Broadcast a task-related event (task_created, task_completed).
        """
        await self.broadcast(org_id, {
            "type": "task_update",
            "event_type": event_type,
            "task": task_data,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    # ── Heartbeat ────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Periodically ping all connections and remove dead ones."""
        while self.total_connections > 0:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            await self._ping_all()

        logger.debug("[WS] Heartbeat loop stopped — no active connections.")

    async def _ping_all(self):
        """Send a ping frame to all connections, clean up dead ones."""
        now = datetime.now(timezone.utc)
        dead_pairs: list[tuple[str, WebSocket]] = []

        for org_id, conns in list(self._connections.items()):
            for ws, info in list(conns.items()):
                # Check pong timeout
                elapsed = (now - info.last_pong).total_seconds()
                if elapsed > self.PONG_TIMEOUT:
                    dead_pairs.append((org_id, ws))
                    continue

                try:
                    await ws.send_text(json.dumps({
                        "type": "ping",
                        "server_time": now.isoformat(),
                    }))
                except Exception:
                    dead_pairs.append((org_id, ws))

        # Clean up dead connections
        for org_id, ws in dead_pairs:
            if org_id in self._connections:
                self._connections[org_id].pop(ws, None)
                if not self._connections[org_id]:
                    del self._connections[org_id]
            logger.debug("[WS] Cleaned up dead/timed-out connection for org=%s", org_id)

    def handle_pong(self, websocket: WebSocket, org_id: str):
        """Update the last_pong time when a client responds."""
        if org_id in self._connections and websocket in self._connections[org_id]:
            self._connections[org_id][websocket].last_pong = datetime.now(timezone.utc)

    # ── Stats ────────────────────────────────────────────────────────────────

    @property
    def total_connections(self) -> int:
        """Total active connections across all orgs."""
        return sum(len(conns) for conns in self._connections.values())

    def get_stats(self) -> dict[str, Any]:
        """Return connection stats for monitoring."""
        per_org = {}
        for org_id, conns in self._connections.items():
            per_org[org_id] = {
                "connections": len(conns),
                "total_messages": sum(c.message_count for c in conns.values()),
                "oldest_connection": min(
                    (c.connected_at for c in conns.values()),
                    default=None,
                ),
            }

        return {
            "total_connections": self.total_connections,
            "total_orgs": len(self._connections),
            "per_org": per_org,
        }

    def get_org_connection_count(self, org_id: str) -> int:
        """Return the number of active connections for an org."""
        return len(self._connections.get(org_id, {}))

    # ── Internal ─────────────────────────────────────────────────────────────

    async def _send_safe(self, ws: WebSocket, data: dict):
        """Send JSON to a websocket, swallowing errors."""
        try:
            await ws.send_text(json.dumps(data, default=str))
        except Exception:
            pass


# Singleton instance used across the application
manager = ConnectionManager()
