"""
Task routes — fetch pending approval tasks by role.

Role access:
  /tasks/gm       → GM+
  /tasks/director → DIRECTOR+
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, require_role, Role
from app.schemas import DealResponse
from app.services.task_service import get_tasks

router = APIRouter(tags=["tasks"])


def _task_to_response(task) -> dict:
    """Convert a Task ORM object to a serializable dict."""
    deal_data = None
    if task.deal:
        deal_data = DealResponse.model_validate(task.deal).model_dump()
    return {
        "id": task.id,
        "deal_id": task.deal_id,
        "assigned_to_role": task.assigned_to_role,
        "status": task.status,
        "created_at": task.created_at,
        "completed_at": task.completed_at,
        "deal": deal_data,
    }


@router.get("/tasks/gm")
def gm_tasks(
    auth: AuthContext = Depends(require_role(Role.GM)),
    db: Session = Depends(get_db),
):
    """GM pending approval tasks. Requires GM role or higher."""
    tasks = get_tasks(db, auth.org_id, "GM")
    return [_task_to_response(t) for t in tasks]


@router.get("/tasks/director")
def director_tasks(
    auth: AuthContext = Depends(require_role(Role.DIRECTOR)),
    db: Session = Depends(get_db),
):
    """Director pending approval tasks. Requires DIRECTOR role or higher."""
    tasks = get_tasks(db, auth.org_id, "DIRECTOR")
    return [_task_to_response(t) for t in tasks]
