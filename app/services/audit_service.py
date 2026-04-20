"""
Audit Service — immutable org-wide action log.

Every destructive or significant action (DELETE, RESTORE, APPROVE, REJECT,
CREATE, UPDATE) is written to audit_logs.  The table is append-only by
convention: no row is ever updated or deleted.

Usage:
    from app.services.audit_service import log_action

    log_action(
        db, org_id=auth.org_id,
        action_type="DELETE",
        entity_type="deal",
        entity_id=deal.id,
        performed_by=auth.user_id,
        performed_by_email=auth.email,
        ip_address=request.client.host,
        previous_data={"status": deal.status, ...},
    )
    db.commit()   # caller is responsible for the commit
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog

logger = logging.getLogger(__name__)

# ── Valid action / entity constants ───────────────────────────────────────────

class Action:
    CREATE  = "CREATE"
    UPDATE  = "UPDATE"
    DELETE  = "DELETE"
    RESTORE = "RESTORE"
    APPROVE = "APPROVE"
    REJECT  = "REJECT"
    LOGIN   = "LOGIN"
    UPLOAD  = "UPLOAD"


class Entity:
    DEAL    = "deal"
    USER    = "user"
    PRICING = "pricing"
    ORG     = "org"


# ─────────────────────────────────────────────────────────────────────────────

def log_action(
    db: Session,
    org_id: str,
    action_type: str,
    entity_type: str,
    entity_id: str,
    performed_by: Optional[str] = None,
    performed_by_email: Optional[str] = None,
    ip_address: Optional[str] = None,
    previous_data: Optional[dict] = None,
    new_data: Optional[dict] = None,
    note: Optional[str] = None,
) -> AuditLog:
    """
    Append one entry to audit_logs.

    Does NOT commit — the caller must call db.commit() after all related
    changes so the audit entry and the business change land in the same
    transaction.
    """
    entry = AuditLog(
        organization_id=org_id,
        action_type=action_type,
        entity_type=entity_type,
        entity_id=entity_id,
        performed_by=performed_by,
        performed_by_email=performed_by_email,
        ip_address=ip_address,
        previous_data=previous_data,
        new_data=new_data,
        note=note,
    )
    db.add(entry)
    logger.info(
        "AUDIT %s %s/%s by %s",
        action_type, entity_type, entity_id, performed_by_email or performed_by or "system",
    )
    return entry


def get_audit_logs(
    db: Session,
    org_id: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action_type: Optional[str] = None,
    performed_by: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
) -> list[AuditLog]:
    """Query audit logs with optional filters."""
    q = db.query(AuditLog).filter(AuditLog.organization_id == org_id)
    if entity_type:
        q = q.filter(AuditLog.entity_type == entity_type)
    if entity_id:
        q = q.filter(AuditLog.entity_id == entity_id)
    if action_type:
        q = q.filter(AuditLog.action_type == action_type)
    if performed_by:
        q = q.filter(AuditLog.performed_by == performed_by)
    return (
        q.order_by(AuditLog.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
