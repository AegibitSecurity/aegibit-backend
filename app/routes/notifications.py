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

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_current_user
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
