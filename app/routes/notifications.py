"""
Notification routes — list, read, and manage notifications.

Endpoints
---------
GET   /notifications                    List latest notifications (unread first)
GET   /notifications/unread-count       Get count of unread notifications
POST  /notifications/{id}/read          Mark one notification as read
POST  /notifications/mark-all-read      Mark all notifications as read

Role access: SALES+ (all authenticated users)
"""

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.models import Deal
from app.schemas import NotificationResponse
from app.services.notification_service import (
    get_notifications,
    get_unread_count,
    mark_read,
    mark_all_read,
)

router = APIRouter(tags=["notifications"])


@router.get("/notifications", response_model=list[NotificationResponse])
def list_notifications(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Fetch latest 50 notifications, unread first."""
    return get_notifications(db, auth.org_id)


@router.get("/notifications/unread-count")
def unread_count(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the number of unread notifications."""
    return {"unread_count": get_unread_count(db, auth.org_id)}


@router.post("/notifications/{notification_id}/read")
def read_notification(
    notification_id: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark a single notification as read."""
    found = mark_read(db, notification_id)
    return {"ok": found}


@router.post("/notifications/mark-all-read")
def read_all_notifications(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Mark all unread notifications as read for the organization."""
    count = mark_all_read(db, auth.org_id)
    return {"ok": True, "marked_read": count}


# ── Upcoming Deliveries ───────────────────────────────────────────────────────

@router.get("/notifications/upcoming-deliveries")
def upcoming_deliveries(
    days: int = Query(3, ge=1, le=7, description="Look-ahead window in days"),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return approved deals whose delivery_date falls within the next N days.
    Strictly scoped to auth.org_id — never returns another tenant's data.

    Response:
      [{ message, delivery_date, days_remaining, customer, deal_id, details }]
    """
    now    = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days)

    deals = (
        db.query(Deal)
        .filter(
            Deal.organization_id == auth.org_id,   # ← always from JWT
            Deal.is_deleted       == False,
            Deal.status           == "APPROVED",
            Deal.delivery_date    != None,
            Deal.delivery_date    >= now,
            Deal.delivery_date    <= cutoff,
        )
        .order_by(Deal.delivery_date.asc())
        .all()
    )

    result = []
    for d in deals:
        # Naive delivery_date stored without tz info in SQLite dev; handle both
        dd = d.delivery_date
        if dd.tzinfo is None:
            dd = dd.replace(tzinfo=timezone.utc)
        delta      = dd - now
        days_left  = max(delta.days, 0)
        day_label  = "today" if days_left == 0 else (
                     "tomorrow" if days_left == 1 else f"in {days_left} days"
                     )

        result.append({
            "message":        f"{d.customer_name} — delivery {day_label} ({d.variant})",
            "delivery_date":  dd.isoformat(),
            "days_remaining": days_left,
            "customer":       d.customer_name,
            "deal_id":        d.id,
            "details": {
                "variant":      d.variant,
                "amount":       d.final_price,
                "mobile":       d.mobile or d.customer_phone,
                "chassis_no":   d.chassis_no,
                "status":       d.status,
                "salesperson":  d.rse_name,
            },
        })

    return result
