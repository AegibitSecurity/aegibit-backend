"""
Dashboard route — aggregate summary stats.

Performance: all deal metrics are computed in ONE SQL aggregation (was 9 queries).
Responses are cached per-org for 30 seconds (TTL cache, no external dependency).

Role access: SALES+ (all authenticated users)
"""

import time
import logging
from datetime import date

from fastapi import APIRouter, Depends
from fastapi.responses import ORJSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.models import Deal, Task

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dashboard"])

# ── Per-org TTL cache (30 seconds) ────────────────────────────────────────────
# Avoids hammering the DB when multiple clients poll the dashboard in parallel.
# Keyed by org_id. Invalidated automatically on TTL expiry.
_CACHE_TTL = 30.0  # seconds
_cache: dict[str, tuple[float, dict]] = {}


def _cache_get(org_id: str) -> dict | None:
    entry = _cache.get(org_id)
    if entry and (time.monotonic() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(org_id: str, data: dict) -> None:
    _cache[org_id] = (time.monotonic(), data)


def invalidate_dashboard_cache(org_id: str) -> None:
    """Call this from deal/approval mutation routes to force a fresh read."""
    _cache.pop(org_id, None)


# ── Route ─────────────────────────────────────────────────────────────────────

@router.get("/dashboard/summary")
def summary(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    cached = _cache_get(auth.org_id)
    if cached:
        return ORJSONResponse(cached)

    today = date.today()

    # ── Query 1: all deal metrics in a single aggregation ─────────────────────
    # Replaces 7 separate .count()/.scalar() calls that each round-tripped the DB.
    stats = (
        db.query(
            func.count().label("total"),
            func.count(case((Deal.status == "APPROVED", 1))).label("approved"),
            func.count(case((Deal.status == "PENDING",  1))).label("pending"),
            func.count(case((Deal.status == "REJECTED", 1))).label("rejected"),
            func.coalesce(func.avg(Deal.margin_percent), 0).label("avg_margin"),
            func.count(case((Deal.risk_level == "HIGH",  1))).label("high_risk"),
            func.coalesce(
                func.sum(
                    case((
                        and_(
                            Deal.status == "APPROVED",
                            func.date(Deal.created_at) == today,
                        ),
                        Deal.final_price,
                    ))
                ),
                0,
            ).label("revenue_today"),
        )
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted == False,
        )
        .one()
    )

    # ── Query 2: pending task counts in a single join ─────────────────────────
    # Replaces 2 separate Task queries each with a Deal join.
    tasks = (
        db.query(
            func.count(case((Task.assigned_to_role == "GM",       1))).label("gm"),
            func.count(case((Task.assigned_to_role == "DIRECTOR", 1))).label("director"),
        )
        .join(Deal, Task.deal_id == Deal.id)
        .filter(
            Deal.organization_id == auth.org_id,
            Deal.is_deleted == False,
            Task.status == "PENDING",
        )
        .one()
    )

    result = {
        "total_deals":           stats.total,
        "approved":              stats.approved,
        "pending":               stats.pending,
        "rejected":              stats.rejected,
        "avg_margin":            round(float(stats.avg_margin), 2),
        "high_risk":             stats.high_risk,
        "revenue_today":         float(stats.revenue_today),
        "pending_gm_tasks":      tasks.gm,
        "pending_director_tasks": tasks.director,
    }

    _cache_set(auth.org_id, result)
    return ORJSONResponse(result)
