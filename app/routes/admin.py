"""
Admin Recovery & Audit Routes — ADMIN role only.

Endpoints:
  GET  /admin/recovery/deleted-deals       list soft-deleted deals
  POST /admin/recovery/bulk-restore        restore multiple deals at once
  GET  /admin/audit/verify-chain           verify hash chain integrity for this org
  GET  /admin/audit/anomalies              mass-delete anomaly detection
  GET  /admin/backup/status                last backup metadata (from env/config)
"""

from __future__ import annotations

from typing import Optional, List
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import get_current_user, AuthContext, require_role
from app.models import Deal, AuditLog
from app.schemas import DealResponse, AuditChainVerifyResponse
from app.services.audit_service import (
    verify_chain,
    detect_delete_anomalies,
    log_action,
    Action,
    Entity,
)
from app.config import settings

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_admin(auth: AuthContext = Depends(get_current_user)) -> AuthContext:
    if auth.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Admin access required")
    return auth


# ── Deleted Deals ─────────────────────────────────────────────────────────────

@router.get("/recovery/deleted-deals")
def list_deleted_deals(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(_require_admin),
):
    """Return all soft-deleted deals for this org, most recently deleted first."""
    rows = (
        db.query(Deal)
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted == True,
        )
        .order_by(Deal.deleted_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": len(rows),
        "deals": [DealResponse.model_validate(r) for r in rows],
    }


# ── Bulk Restore ──────────────────────────────────────────────────────────────

@router.post("/recovery/bulk-restore")
def bulk_restore_deals(
    payload: dict,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(_require_admin),
):
    """
    Restore one or more soft-deleted deals.

    Body: { "deal_ids": ["uuid1", "uuid2", ...], "reason": "optional note" }
    """
    deal_ids: List[str] = payload.get("deal_ids", [])
    reason: str = payload.get("reason", "Bulk restore by admin")

    if not deal_ids:
        raise HTTPException(status_code=400, detail="deal_ids list is required")
    if len(deal_ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 deals per bulk restore")

    restored = []
    not_found = []

    for deal_id in deal_ids:
        deal = (
            db.query(Deal)
            .filter(
                Deal.id == deal_id,
                Deal.organization_id == auth.org_id,
                Deal.is_deleted == True,
            )
            .first()
        )
        if not deal:
            not_found.append(deal_id)
            continue

        previous = {
            "is_deleted": True,
            "deleted_at": deal.deleted_at.isoformat() if deal.deleted_at else None,
        }
        deal.is_deleted = False
        deal.deleted_at = None
        deal.deleted_by = None

        log_action(
            db,
            org_id=auth.org_id,
            action_type=Action.RESTORE,
            entity_type=Entity.DEAL,
            entity_id=deal.id,
            performed_by=auth.user_id,
            performed_by_email=auth.email,
            previous_data=previous,
            new_data={"is_deleted": False},
            note=reason,
        )
        restored.append(deal_id)

    db.commit()
    return {
        "restored": restored,
        "not_found": not_found,
        "restored_count": len(restored),
    }


# ── Verify Hash Chain ─────────────────────────────────────────────────────────

@router.get("/audit/verify-chain", response_model=AuditChainVerifyResponse)
def verify_audit_chain(
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(_require_admin),
):
    """
    Walk every audit log entry for this org and verify the SHA-256 hash chain.
    Returns { ok, total, verified, tampered[], checked_at }.
    A non-empty tampered list means records were modified after being written.
    """
    result = verify_chain(db, auth.org_id)
    return result


# ── Anomaly Detection ─────────────────────────────────────────────────────────

@router.get("/audit/anomalies")
def get_anomalies(
    window_minutes: int = Query(10, ge=1, le=60),
    threshold: int = Query(5, ge=2, le=100),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(_require_admin),
):
    """
    Detect users who performed an unusual number of DELETE actions within
    the given time window. Default: >5 deletes in 10 minutes.
    """
    anomalies = detect_delete_anomalies(
        db, auth.org_id,
        window_minutes=window_minutes,
        threshold=threshold,
    )
    return {
        "anomalies": anomalies,
        "window_minutes": window_minutes,
        "threshold": threshold,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Backup Status ─────────────────────────────────────────────────────────────

@router.get("/backup/status")
def backup_status(
    auth: AuthContext = Depends(_require_admin),
):
    """
    Return backup configuration status. Actual last-backup metadata
    is stored in the LAST_BACKUP_AT env var by the backup cron script.
    """
    import os
    return {
        "s3_configured": bool(os.getenv("S3_BUCKET_NAME")),
        "s3_bucket": os.getenv("S3_BUCKET_NAME", "not configured"),
        "s3_endpoint": os.getenv("S3_ENDPOINT_URL", "AWS S3 (default)"),
        "last_backup_at": os.getenv("LAST_BACKUP_AT", "unknown"),
        "backup_retention_days": int(os.getenv("BACKUP_RETENTION_DAYS", "30")),
        "alert_webhook_configured": bool(os.getenv("ALERT_WEBHOOK_URL")),
    }
