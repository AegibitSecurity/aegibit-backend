"""
Dashboard route — aggregate summary stats.

Role access: SALES+ (all authenticated users)
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.models import Deal, Task
from app.schemas import DashboardSummary

router = APIRouter(tags=["dashboard"])


@router.get("/dashboard/summary", response_model=DashboardSummary)
def summary(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Only count non-deleted deals
    base = db.query(Deal).filter(Deal.organization_id == auth.org_id, Deal.is_deleted == False)

    total = base.count()
    approved = base.filter(Deal.status == "APPROVED").count()
    pending = base.filter(Deal.status == "PENDING").count()
    rejected = base.filter(Deal.status == "REJECTED").count()

    avg_margin_row = base.with_entities(func.avg(Deal.margin_percent)).scalar()
    avg_margin = round(float(avg_margin_row or 0), 2)

    # Calculate high risk deals
    high_risk = base.filter(Deal.risk_level == "HIGH").count()

    # Calculate revenue today (sum of approved deals from today)
    from datetime import datetime, date
    today = date.today()
    revenue_today_row = base.filter(
        Deal.status == "APPROVED",
        func.date(Deal.created_at) == today
    ).with_entities(func.sum(Deal.final_price)).scalar()
    revenue_today = float(revenue_today_row or 0)

    pending_gm = (
        db.query(Task)
        .join(Deal)
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted == False,
            Task.assigned_to_role == "GM",
            Task.status == "PENDING",
        )
        .count()
    )
    pending_director = (
        db.query(Task)
        .join(Deal)
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted == False,
            Task.assigned_to_role == "DIRECTOR",
            Task.status == "PENDING",
        )
        .count()
    )

    return DashboardSummary(
        total_deals=total,
        approved=approved,
        pending=pending,
        rejected=rejected,
        avg_margin=avg_margin,
        high_risk=high_risk,
        revenue_today=revenue_today,
        pending_gm_tasks=pending_gm,
        pending_director_tasks=pending_director,
    )
