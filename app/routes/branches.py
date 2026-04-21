"""
Branch routes — list branches, create branch.

Endpoints:
  GET  /branches         → All branches for this org (any authenticated user)
  POST /branches         → Create branch (ADMIN only)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from app.database import get_db
from app.auth import AuthContext, get_current_user, require_admin
from app.models import Branch

router = APIRouter(tags=["branches"])


class BranchResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    is_head_office: bool
    created_at: datetime

    class Config:
        from_attributes = True


class CreateBranchRequest(BaseModel):
    name: str
    is_head_office: bool = False


@router.get("/branches", response_model=list[BranchResponse])
def list_branches(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return all branches for the authenticated user's organization."""
    return (
        db.query(Branch)
        .filter(Branch.organization_id == auth.org_id)
        .order_by(Branch.is_head_office.desc(), Branch.name)
        .all()
    )


@router.post("/branches", response_model=BranchResponse, status_code=201)
def create_branch(
    body: CreateBranchRequest,
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """Create a new branch. ADMIN only."""
    existing = db.query(Branch).filter(
        Branch.organization_id == auth.org_id,
        Branch.name == body.name.strip().upper(),
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Branch '{body.name}' already exists")

    if body.is_head_office:
        hq_exists = db.query(Branch).filter(
            Branch.organization_id == auth.org_id,
            Branch.is_head_office == True,
        ).first()
        if hq_exists:
            raise HTTPException(status_code=409, detail="A head-office branch already exists")

    branch = Branch(
        organization_id=auth.org_id,
        name=body.name.strip().upper(),
        is_head_office=body.is_head_office,
    )
    db.add(branch)
    db.commit()
    db.refresh(branch)
    return branch
