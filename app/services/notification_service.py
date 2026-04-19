"""
Notification Service — unified event system for AEGIBIT Flow.

Provides a single `trigger_event()` function that:
  1. Generates a human-readable title + message
  2. Persists to the notifications table (in-app channel)
  3. Broadcasts via WebSocket (real-time channel)

Event Types
-----------
  DEAL_CREATED         A new deal was submitted
  DEAL_AUTO_APPROVED   Profit Guard auto-approved a deal
  GM_APPROVED          GM approved a deal
  GM_ESCALATED         GM escalated a deal to Director
  DIRECTOR_APPROVED    Director approved a deal
  DEAL_REJECTED        A deal was rejected
  PRICING_UPLOADED     New pricing batch uploaded

Integration
-----------
  The notification service is the ONLY module that should call
  ws_manager.broadcast. All other services go through trigger_event()
  or the convenience wrappers (notify_deal_created, etc.).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import Notification
from app.services.websocket_manager import manager as ws_manager

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Event Registry — defines all known event types with templates
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EventDefinition:
    """Template for a notification event type."""
    event_type: str
    title_template: str       # Python str.format() template
    message_template: str     # Python str.format() template
    ws_event_type: str        # WebSocket event type key
    severity: str = "INFO"    # INFO | WARNING | CRITICAL
    category: str = "deal"    # deal | task | system


# Registry of all event types
EVENT_REGISTRY: dict[str, EventDefinition] = {}


def _register(defn: EventDefinition):
    EVENT_REGISTRY[defn.event_type] = defn


_register(EventDefinition(
    event_type="DEAL_CREATED",
    title_template="New Deal Created",
    message_template="Deal for {customer} ({variant}) — {decision}. Margin: {margin_display}.",
    ws_event_type="deal_created",
    severity="INFO",
    category="deal",
))

_register(EventDefinition(
    event_type="DEAL_AUTO_APPROVED",
    title_template="Deal Auto-Approved",
    message_template="Deal for {customer} ({variant}) was auto-approved. Margin: {margin_display}.",
    ws_event_type="deal_approved",
    severity="INFO",
    category="deal",
))

_register(EventDefinition(
    event_type="GM_APPROVED",
    title_template="Deal Approved by GM",
    message_template="GM approved deal for {customer} ({variant}).",
    ws_event_type="deal_approved",
    severity="INFO",
    category="deal",
))

_register(EventDefinition(
    event_type="GM_ESCALATED",
    title_template="Deal Escalated to Director",
    message_template="GM escalated deal for {customer} ({variant}) to Director — discount exceeds GM limit.",
    ws_event_type="status_changed",
    severity="WARNING",
    category="deal",
))

_register(EventDefinition(
    event_type="DIRECTOR_APPROVED",
    title_template="Deal Approved by Director",
    message_template="Director approved deal for {customer} ({variant}).",
    ws_event_type="deal_approved",
    severity="INFO",
    category="deal",
))

_register(EventDefinition(
    event_type="DEAL_REJECTED",
    title_template="Deal Rejected",
    message_template="Deal for {customer} ({variant}) was rejected by {actor}.",
    ws_event_type="deal_rejected",
    severity="WARNING",
    category="deal",
))

_register(EventDefinition(
    event_type="PRICING_UPLOADED",
    title_template="Pricing Updated",
    message_template="New pricing batch uploaded: {inserted} variants inserted, {skipped} skipped.",
    ws_event_type="pricing_uploaded",
    severity="INFO",
    category="system",
))

_register(EventDefinition(
    event_type="TASK_CREATED",
    title_template="New Approval Task",
    message_template="New {role} approval task created for deal {deal_id_short}.",
    ws_event_type="task_update",
    severity="INFO",
    category="task",
))

_register(EventDefinition(
    event_type="TASK_COMPLETED",
    title_template="Task Completed",
    message_template="{role} task completed for deal {deal_id_short}.",
    ws_event_type="task_update",
    severity="INFO",
    category="task",
))


# ─────────────────────────────────────────────────────────────────────────────
# Core: trigger_event  (the single entry point)
# ─────────────────────────────────────────────────────────────────────────────

async def trigger_event(
    db: Session,
    org_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    deal_dict: Optional[dict] = None,
) -> Notification:
    """
    Fire an event through all notification channels.

    Parameters
    ----------
    db          : Active DB session
    org_id      : Organization scope
    event_type  : One of the registered event types (see EVENT_REGISTRY)
    payload     : Arbitrary data attached to the notification.
                  Also used to fill title/message templates.
    deal_dict   : Optional serialized deal for WebSocket broadcast.

    Returns
    -------
    The persisted Notification row.
    """
    defn = EVENT_REGISTRY.get(event_type)

    if defn is None:
        logger.warning("Unknown event type '%s' — storing raw, no template.", event_type)
        title = event_type.replace("_", " ").title()
        message = str(payload)
        ws_event_type = event_type.lower()
        severity = "INFO"
        category = "system"
    else:
        title = _safe_format(defn.title_template, payload)
        message = _safe_format(defn.message_template, payload)
        ws_event_type = defn.ws_event_type
        severity = defn.severity
        category = defn.category

    # ── 1. In-app: persist to DB ──────────────────────────────────────────────
    notif = _persist_notification(
        db, org_id, event_type,
        payload={
            **payload,
            "title": title,
            "message": message,
            "severity": severity,
            "category": category,
        },
    )

    # ── 2. WebSocket: broadcast using typed helpers ──────────────────────────
    if category == "deal" and deal_dict is not None:
        await ws_manager.broadcast_deal_event(
            org_id,
            event_type,
            deal=deal_dict,
            notification_id=notif.id,
            title=title,
            message=message,
            severity=severity,
        )
    elif category == "task":
        await ws_manager.broadcast_task_event(
            org_id,
            event_type,
            task_data={
                **payload,
                "notification_id": notif.id,
                "title": title,
                "message": message,
            },
        )
    else:
        # Generic broadcast (system events, etc.)
        ws_payload: dict[str, Any] = {
            "type": ws_event_type,
            "event_type": event_type,
            "notification_id": notif.id,
            "title": title,
            "message": message,
            "severity": severity,
            "payload": payload,
            "created_at": notif.created_at.isoformat() if notif.created_at else None,
        }
        if deal_dict is not None:
            ws_payload["deal"] = deal_dict
        await ws_manager.broadcast(org_id, ws_payload)

    logger.info(
        "[%s] %s: %s (org=%s, notif=%s, ws_clients=%d)",
        severity, event_type, message, org_id, notif.id,
        ws_manager.get_org_connection_count(org_id),
    )

    return notif


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrappers (keep deal_service clean)
# ─────────────────────────────────────────────────────────────────────────────

def _margin_display(margin: float, margin_pct: float) -> str:
    """Format margin for human display."""
    sign = "+" if margin >= 0 else ""
    return f"{sign}{margin:,.0f} ({margin_pct:+.1f}%)"


async def notify_deal_created(
    db: Session, org_id: str, deal_dict: dict, decision: str,
):
    """Fire DEAL_CREATED (and optionally DEAL_AUTO_APPROVED)."""
    margin_disp = _margin_display(
        deal_dict.get("margin", 0),
        deal_dict.get("margin_percent", 0),
    )

    await trigger_event(db, org_id, "DEAL_CREATED", {
        "deal_id": deal_dict.get("id"),
        "customer": deal_dict.get("customer_name", ""),
        "variant": deal_dict.get("variant", ""),
        "decision": decision,
        "status": deal_dict.get("status", ""),
        "risk": deal_dict.get("risk_level", ""),
        "margin_display": margin_disp,
    }, deal_dict=deal_dict)

    # If auto-approved, fire a second event
    if decision == "AUTO_APPROVE":
        await trigger_event(db, org_id, "DEAL_AUTO_APPROVED", {
            "deal_id": deal_dict.get("id"),
            "customer": deal_dict.get("customer_name", ""),
            "variant": deal_dict.get("variant", ""),
            "margin_display": margin_disp,
        }, deal_dict=deal_dict)


async def notify_gm_approved(db: Session, org_id: str, deal_dict: dict):
    """Fire GM_APPROVED event."""
    await trigger_event(db, org_id, "GM_APPROVED", {
        "deal_id": deal_dict.get("id"),
        "customer": deal_dict.get("customer_name", ""),
        "variant": deal_dict.get("variant", ""),
    }, deal_dict=deal_dict)


async def notify_gm_escalated(db: Session, org_id: str, deal_dict: dict):
    """Fire GM_ESCALATED event."""
    await trigger_event(db, org_id, "GM_ESCALATED", {
        "deal_id": deal_dict.get("id"),
        "customer": deal_dict.get("customer_name", ""),
        "variant": deal_dict.get("variant", ""),
    }, deal_dict=deal_dict)


async def notify_director_approved(db: Session, org_id: str, deal_dict: dict):
    """Fire DIRECTOR_APPROVED event."""
    await trigger_event(db, org_id, "DIRECTOR_APPROVED", {
        "deal_id": deal_dict.get("id"),
        "customer": deal_dict.get("customer_name", ""),
        "variant": deal_dict.get("variant", ""),
    }, deal_dict=deal_dict)


async def notify_deal_rejected(
    db: Session, org_id: str, deal_dict: dict, actor_role: str,
):
    """Fire DEAL_REJECTED event."""
    await trigger_event(db, org_id, "DEAL_REJECTED", {
        "deal_id": deal_dict.get("id"),
        "customer": deal_dict.get("customer_name", ""),
        "variant": deal_dict.get("variant", ""),
        "actor": actor_role,
    }, deal_dict=deal_dict)


async def notify_task_created(
    db: Session, org_id: str, deal_id: str, role: str,
):
    """Fire TASK_CREATED event when a new approval task is generated."""
    await trigger_event(db, org_id, "TASK_CREATED", {
        "deal_id": deal_id,
        "deal_id_short": deal_id[:8],
        "role": role,
    })


async def notify_task_completed(
    db: Session, org_id: str, deal_id: str, role: str,
):
    """Fire TASK_COMPLETED event when a task is resolved."""
    await trigger_event(db, org_id, "TASK_COMPLETED", {
        "deal_id": deal_id,
        "deal_id_short": deal_id[:8],
        "role": role,
    })


async def notify_pricing_uploaded(
    db: Session, org_id: str, inserted: int, skipped: int, batch_id: str,
):
    """Fire PRICING_UPLOADED event."""
    await trigger_event(db, org_id, "PRICING_UPLOADED", {
        "inserted": inserted,
        "skipped": skipped,
        "upload_batch": batch_id,
    })


# ─────────────────────────────────────────────────────────────────────────────
# DB operations
# ─────────────────────────────────────────────────────────────────────────────

def _persist_notification(
    db: Session,
    org_id: str,
    event_type: str,
    payload: dict,
) -> Notification:
    """Insert a notification row and return it."""
    notif = Notification(
        organization_id=org_id,
        event_type=event_type,
        payload=payload,
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif


def get_notifications(
    db: Session, org_id: str, limit: int = 50,
) -> list[Notification]:
    """Fetch latest notifications: unread first, then by recency."""
    return (
        db.query(Notification)
        .filter(Notification.organization_id == org_id)
        .order_by(
            Notification.status.asc(),      # UNREAD before READ
            Notification.created_at.desc(),
        )
        .limit(limit)
        .all()
    )


def get_unread_count(db: Session, org_id: str) -> int:
    """Return the count of unread notifications for an org."""
    return (
        db.query(Notification)
        .filter(
            Notification.organization_id == org_id,
            Notification.status == "UNREAD",
        )
        .count()
    )


def mark_read(db: Session, notification_id: str) -> bool:
    """Mark a single notification as read. Returns True if found."""
    notif = db.query(Notification).filter(Notification.id == notification_id).first()
    if notif and notif.status != "READ":
        notif.status = "READ"
        db.commit()
        return True
    return False


def mark_all_read(db: Session, org_id: str) -> int:
    """Mark all unread notifications as read. Returns count updated."""
    count = (
        db.query(Notification)
        .filter(
            Notification.organization_id == org_id,
            Notification.status == "UNREAD",
        )
        .update({"status": "READ"})
    )
    db.commit()
    return count


# ─────────────────────────────────────────────────────────────────────────────
# Template helper
# ─────────────────────────────────────────────────────────────────────────────

def _safe_format(template: str, data: dict) -> str:
    """Format a template string, substituting missing keys with '?'."""
    class SafeDict(dict):
        def __missing__(self, key):
            return "?"
    return template.format_map(SafeDict(data))
