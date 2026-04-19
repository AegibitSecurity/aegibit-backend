"""
Task Service — manages approval tasks routed to GM / Director.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from app.models import Task, Deal


def create_task(db: Session, deal_id: str, role: str) -> Task:
    """Create an approval task, unless a duplicate pending task already exists."""
    existing = (
        db.query(Task)
        .filter(
            Task.deal_id == deal_id,
            Task.assigned_to_role == role,
            Task.status == "PENDING",
        )
        .first()
    )
    if existing:
        return existing

    task = Task(deal_id=deal_id, assigned_to_role=role)
    db.add(task)
    db.flush()
    return task


def complete_task(db: Session, deal_id: str, role: str):
    """Mark a pending task as completed."""
    task = (
        db.query(Task)
        .filter(
            Task.deal_id == deal_id,
            Task.assigned_to_role == role,
            Task.status == "PENDING",
        )
        .first()
    )
    if task:
        task.status = "COMPLETED"
        task.completed_at = datetime.now(timezone.utc)


def get_tasks(db: Session, org_id: str, role: str) -> list[Task]:
    """Return all pending tasks for a given role within an org."""
    return (
        db.query(Task)
        .join(Deal, Task.deal_id == Deal.id)
        .options(joinedload(Task.deal))
        .filter(
            Deal.organization_id == org_id,
            Task.assigned_to_role == role,
            Task.status == "PENDING",
        )
        .order_by(Task.created_at.desc())
        .all()
    )
