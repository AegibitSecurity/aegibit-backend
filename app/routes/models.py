"""
Model → Variant hierarchy routes.

Endpoints
---------
GET /models              Unique model list for the org
GET /models/{model}/variants   All variants under a given model

Role access: SALES+ (all authenticated users)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import distinct

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.models import CarModel

router = APIRouter(tags=["models"])


@router.get("/models")
def list_models(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all unique model names for the organization's active pricing.

    Response: [{"model": "New Punch"}, {"model": "Harrier"}, ...]
    """
    rows = (
        db.query(distinct(CarModel.model_name))
        .filter(
            CarModel.organization_id == auth.org_id,
            CarModel.is_active == True,
            CarModel.model_name.isnot(None),
        )
        .order_by(CarModel.model_name)
        .all()
    )
    return [{"model": r[0]} for r in rows]


@router.get("/models/{model_name}/variants")
def list_variants_by_model(
    model_name: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Return all active variants grouped under a specific model.

    Response: {"model": "New Punch", "variants": [...]}
    """
    cars = (
        db.query(CarModel)
        .filter(
            CarModel.organization_id == auth.org_id,
            CarModel.model_name == model_name,
            CarModel.is_active == True,
        )
        .order_by(CarModel.variant)
        .all()
    )

    if not cars:
        raise HTTPException(status_code=404, detail=f"No variants found for model '{model_name}'")

    return {
        "model": model_name,
        "variants": [
            {
                "variant": c.variant,
                "ex_showroom_price": c.ex_showroom_price,
                "total_5yr": c.total_5yr,
                "total_15yr": c.total_15yr,
                "total_bh": c.total_bh,
            }
            for c in cars
        ],
    }
