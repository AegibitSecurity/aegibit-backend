"""
Audit Service — tamper-proof, append-only org-wide action log.

Hash chain design:
  Each audit entry stores:
    row_hash  = SHA256(id|org_id|action|entity_type|entity_id|by_email|
                       previous_data|new_data|note|created_at|prev_hash)
    prev_hash = row_hash of the immediately preceding entry for this org.
                Genesis entry uses prev_hash = "0" * 64.

  Any modification to a stored row causes row_hash to no longer match its
  recomputed value → tampering is immediately detectable via verify_chain().

  Any re-ordering of rows is detectable because prev_hash references break.

Usage:
    from app.services.audit_service import log_action
    log_action(db, org_id=..., action_type="DELETE", entity_type="deal",
               entity_id=deal.id, performed_by=user_id, ...)
    db.commit()  # caller commits — keeps audit + business change atomic
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.models import AuditLog

logger = logging.getLogger(__name__)

_GENESIS = "0" * 64   # sentinel prev_hash for the first entry per org


# ── Action / entity constants ─────────────────────────────────────────────────

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


# ── Hash chain helpers ────────────────────────────────────────────────────────

def _serialize(value) -> str:
    """Deterministic serialisation for hashing."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return str(value)


def compute_row_hash(entry: AuditLog, prev_hash: str) -> str:
    """
    Compute the SHA-256 hash for one audit entry.
    All fields that constitute the row's identity are included.
    """
    parts = "|".join([
        entry.id or "",
        entry.organization_id or "",
        entry.action_type or "",
        entry.entity_type or "",
        entry.entity_id or "",
        entry.performed_by or "",
        entry.performed_by_email or "",
        entry.ip_address or "",
        _serialize(entry.previous_data),
        _serialize(entry.new_data),
        entry.note or "",
        entry.created_at.isoformat() if entry.created_at else "",
        prev_hash,
    ])
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _get_latest_hash(db: Session, org_id: str) -> str:
    """Return the row_hash of the most recent audit entry for this org, or genesis."""
    latest = (
        db.query(AuditLog.row_hash)
        .filter(AuditLog.organization_id == org_id, AuditLog.row_hash.isnot(None))
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .first()
    )
    return latest[0] if latest else _GENESIS


# ── Core write ────────────────────────────────────────────────────────────────

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
    Append one tamper-evident entry to audit_logs.

    Does NOT commit — caller must call db.commit() so the audit entry and
    the business change land in the same transaction (atomic pair).
    """
    prev_hash = _get_latest_hash(db, org_id)

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
        prev_hash=prev_hash,
    )

    # id and created_at are not set until db.add() + db.flush() assigns defaults,
    # so we flush first, then compute and write the hash.
    db.add(entry)
    db.flush()   # assigns entry.id and entry.created_at

    entry.row_hash = compute_row_hash(entry, prev_hash)

    logger.info(
        "AUDIT %s %s/%s by=%s hash=%s",
        action_type, entity_type, entity_id,
        performed_by_email or performed_by or "system",
        entry.row_hash[:12],
    )
    return entry


# ── Chain verification ────────────────────────────────────────────────────────

def verify_chain(db: Session, org_id: str) -> dict:
    """
    Walk every audit entry for this org in chronological order and verify:
      1. row_hash matches the recomputed hash for that row's data.
      2. prev_hash matches the previous row's row_hash (or genesis for first).

    Returns:
        {
          "ok":            bool,
          "total":         int,     total entries checked
          "verified":      int,     entries that passed
          "tampered":      list,    [{"id": ..., "reason": ...}] for failures
          "checked_at":    str,     ISO timestamp
        }
    """
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.organization_id == org_id)
        .order_by(AuditLog.created_at.asc(), AuditLog.id.asc())
        .all()
    )

    tampered = []
    expected_prev = _GENESIS

    for row in rows:
        # Skip rows written before the hash chain was introduced (row_hash is NULL)
        if row.row_hash is None:
            expected_prev = _GENESIS  # can't chain through a null — reset
            continue

        # Check prev_hash linkage
        if row.prev_hash != expected_prev:
            tampered.append({
                "id":     row.id,
                "reason": f"prev_hash mismatch (expected {expected_prev[:12]}… "
                          f"got {(row.prev_hash or '')[:12]}…)",
            })

        # Recompute and check row_hash
        recomputed = compute_row_hash(row, row.prev_hash or _GENESIS)
        if row.row_hash != recomputed:
            tampered.append({
                "id":     row.id,
                "reason": f"row_hash mismatch (stored {row.row_hash[:12]}… "
                          f"recomputed {recomputed[:12]}…)",
            })

        expected_prev = row.row_hash

    verified = len(rows) - len(tampered)
    return {
        "ok":         len(tampered) == 0,
        "total":      len(rows),
        "verified":   verified,
        "tampered":   tampered,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Anomaly detection ─────────────────────────────────────────────────────────

def detect_delete_anomalies(db: Session, org_id: str, window_minutes: int = 10, threshold: int = 5) -> list[dict]:
    """
    Find users who performed more than `threshold` DELETE actions within
    `window_minutes`.  Returns a list of anomaly dicts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    rows = (
        db.query(AuditLog)
        .filter(
            AuditLog.organization_id == org_id,
            AuditLog.action_type == Action.DELETE,
            AuditLog.created_at >= cutoff,
        )
        .order_by(AuditLog.created_at.asc())
        .all()
    )

    from collections import defaultdict
    by_user: dict = defaultdict(list)
    for r in rows:
        key = r.performed_by_email or r.performed_by or "unknown"
        by_user[key].append(r)

    anomalies = []
    for user, events in by_user.items():
        if len(events) >= threshold:
            anomalies.append({
                "user":       user,
                "count":      len(events),
                "window_min": window_minutes,
                "first_at":   events[0].created_at.isoformat(),
                "last_at":    events[-1].created_at.isoformat(),
                "entity_ids": [e.entity_id for e in events],
            })

    return anomalies


# ── Queries ───────────────────────────────────────────────────────────────────

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
    """Query audit logs with optional filters, most recent first."""
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
