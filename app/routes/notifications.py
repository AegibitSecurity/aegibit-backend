"""
Notification routes — list, read, and manage notifications.

Endpoints
---------
GET   /notifications                    List latest notifications (unread first)
GET   /notifications/unread-count       Get count of unread notifications
POST  /notifications/{id}/read          Mark one notification as read
POST  /notifications/mark-all-read      Mark all notifications as read
GET   /notifications/upcoming-deliveries Approved deals with delivery in next N days

Role access: SALES+ (all authenticated users)
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from fastapi.responses import ORJSONResponse
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

    Delivery logic (correct):
      - Uses naive UTC datetimes to match the TIMESTAMP WITHOUT TIME ZONE column.
      - days_remaining is calculated via calendar date subtraction, NOT
        timedelta.days — that was the root bug (timedelta.days counts 24-hour
        periods, not calendar days, so a delivery tomorrow morning at 08:00
        with now at 23:00 would be 9 hours = delta.days=0 = "today").
      - Past delivery dates (overdue) are excluded from the window.

    Scoped strictly to auth.org_id — never leaks another tenant's data.

    Response:
      [{ message, delivery_date, days_remaining, customer, deal_id, details }]
    """
    # Use naive UTC to match TIMESTAMP WITHOUT TIME ZONE columns in PostgreSQL.
    now_naive  = datetime.utcnow()
    cutoff     = now_naive + timedelta(days=days)
    today_date = now_naive.date()

    branch_filter = []
    if not auth.sees_all_branches() and auth.branch_id:
        branch_filter = [Deal.branch_id == auth.branch_id]

    deals = (
        db.query(Deal)
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted      == False,
            Deal.status          == "APPROVED",
            Deal.delivery_date   != None,
            Deal.delivery_date   >= now_naive,
            Deal.delivery_date   <= cutoff,
            *branch_filter,
        )
        .order_by(Deal.delivery_date.asc())
        # Select only the columns this endpoint actually uses
        .with_entities(
            Deal.id,
            Deal.customer_name,
            Deal.variant,
            Deal.delivery_date,
            Deal.final_price,
            Deal.mobile,
            Deal.customer_phone,
            Deal.chassis_no,
            Deal.status,
            Deal.rse_name,
        )
        .all()
    )

    result = []
    for row in deals:
        # Calendar-day difference — correct regardless of time-of-day.
        # Example: delivery 2024-04-22 08:00, now 2024-04-21 23:00
        #   timedelta.days  = 0  (only 9 hours elapsed) ← OLD BUG
        #   date subtraction = 1 (22 - 21 = 1)          ← CORRECT
        days_left = max((row.delivery_date.date() - today_date).days, 0)

        if   days_left == 0: day_label = "today"
        elif days_left == 1: day_label = "tomorrow"
        else:                day_label = f"in {days_left} days"

        result.append({
            "message":        f"{row.customer_name} — delivery {day_label} ({row.variant})",
            "delivery_date":  row.delivery_date.isoformat(),
            "days_remaining": days_left,
            "customer":       row.customer_name,
            "deal_id":        row.id,
            "details": {
                "variant":     row.variant,
                "amount":      row.final_price,
                "mobile":      row.mobile or row.customer_phone,
                "chassis_no":  row.chassis_no,
                "status":      row.status,
                "salesperson": row.rse_name,
            },
        })

    return ORJSONResponse(result)
