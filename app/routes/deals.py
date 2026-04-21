"""
Deal routes — create, analyze, approve, reject, list.

HTTP status mapping:
  400  →  DealValidationError   (bad input)
  403  →  Insufficient role
  404  →  DealNotFoundError     (variant / deal / config not found)
  422  →  Pydantic validation   (handled by FastAPI automatically)

Role access:
  analyze      → SALES+
  create-deal  → SALES+
  approve-gm   → GM+
  approve-dir  → DIRECTOR+
  reject       → GM+
  list deals   → SALES+
  deal detail  → SALES+
  variants     → SALES+
"""

from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from typing import Annotated, Optional
from pydantic import ValidationError

from app.database import get_db
from app.auth import AuthContext, get_current_user, require_role, Role
from app.limiter import limiter
from app.models import Deal, Task, DealEvent
from app.services import deal_service
from app.services.deal_service import (
    DealValidationError,
    DealNotFoundError,
)
from app.services.audit_service import get_audit_logs
from app.schemas import (
    DealAnalyzeRequest,
    DealAnalyzeResponse,
    DealCreateRequest,
    DealResponse,
    DealDetailResponse,
    DealEventResponse,
    ApprovalRequest,
    ValidationErrorResponse,
    AuditLogResponse,
)

router = APIRouter(tags=["deals"])


def _handle_deal_error(e: Exception):
    """Map service-layer exceptions to HTTP responses."""
    if isinstance(e, DealValidationError):
        raise HTTPException(status_code=400, detail=str(e))
    if isinstance(e, DealNotFoundError):
        raise HTTPException(status_code=404, detail=str(e))
    raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")


# ── POST /deal/analyze — read-only preview ───────────────────────────────────

@router.post("/deal/analyze", response_model=DealAnalyzeResponse)
def analyze(
    req: DealAnalyzeRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Preview a deal's margin, risk, and decision without persisting anything.
    Useful for the frontend "What-if" calculator.
    """
    try:
        return deal_service.analyze_deal(db, auth.org_id, req)
    except (DealValidationError, DealNotFoundError, ValueError) as e:
        _handle_deal_error(e)


# ── POST /create-deal — full creation pipeline ──────────────────────────────

@router.post("/create-deal", response_model=DealResponse, responses={400: {"model": ValidationErrorResponse}})
@limiter.limit("30/minute")
async def create(
    request: Request,
    req: DealCreateRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Create a new deal with STRICT mandatory validation:
      1. Validates ALL required fields (customer, vehicle, CRM, finance, meta)
      2. Validates field formats (mobile, aadhaar, PAN, chassis, engine)
      3. Conditional validation for FINANCE deals
      4. Fetches pricing from DB (only active batch, fuzzy variant match)
      5. Resolves base price by registration type
      6. Runs Profit Guard → margin, risk, decision
      7. Sets deal status + approval_stage
      8. Auto-creates GM / Director task when needed
      9. Fires notification + WebSocket broadcast

    Phone Verification:
      - Phone must be verified before creating deal
      - Fraud rules enforced (max 3 deals/phone/day, 10 deals/salesperson/hour)
    """
    try:
        # Pydantic validation will automatically run here
        # Pass salesperson_id for fraud tracking and customer linking
        return await deal_service.create_deal(db, auth.org_id, req, salesperson_id=auth.user_id)
    except ValidationError as e:
        # Handle Pydantic validation errors
        missing_fields = []
        invalid_fields = {}
        
        for error in e.errors():
            field = error['loc'][0] if error['loc'] else 'unknown'
            msg = error['msg']
            
            if 'required' in msg.lower():
                missing_fields.append(field)
            else:
                invalid_fields[field] = msg
        
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ValidationErrorResponse(
                error="Validation failed",
                missing_fields=missing_fields,
                invalid_fields=invalid_fields
            ).model_dump()
        )
    except (DealValidationError, DealNotFoundError, ValueError) as e:
        _handle_deal_error(e)
    except IntegrityError as e:
        orig = str(getattr(e, "orig", e))
        if "uq_deal_customer_phone" in orig or "customer_phone" in orig:
            raise HTTPException(
                status_code=409,
                detail="Phone number already has an active deal. Each phone can only be used once.",
            )
        raise HTTPException(status_code=409, detail="Duplicate entry — please check your data.")
    except Exception as e:
        import traceback
        print(f"UNEXPECTED ERROR in create-deal: {e}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)}")


# ── POST /approve-gm ────────────────────────────────────────────────────────

@router.post("/approve-gm", response_model=DealResponse)
@limiter.limit("60/minute")
async def approve_gm(
    request: Request,
    req: ApprovalRequest,
    auth: AuthContext = Depends(require_role(Role.GM)),
    db: Session = Depends(get_db),
):
    """GM approves a pending deal. May escalate to Director if discount is too high."""
    try:
        return await deal_service.approve_gm(db, auth.org_id, req.deal_id)
    except (DealValidationError, DealNotFoundError, ValueError) as e:
        _handle_deal_error(e)


# ── POST /approve-director ──────────────────────────────────────────────────

@router.post("/approve-director", response_model=DealResponse)
@limiter.limit("60/minute")
async def approve_director(
    request: Request,
    req: ApprovalRequest,
    auth: AuthContext = Depends(require_role(Role.DIRECTOR)),
    db: Session = Depends(get_db),
):
    """Director approves — final authority. No further escalation."""
    try:
        return await deal_service.approve_director(db, auth.org_id, req.deal_id)
    except (DealValidationError, DealNotFoundError, ValueError) as e:
        _handle_deal_error(e)


# ── POST /reject ────────────────────────────────────────────────────────────

@router.post("/reject", response_model=DealResponse)
@limiter.limit("60/minute")
async def reject(
    request: Request,
    req: ApprovalRequest,
    auth: AuthContext = Depends(require_role(Role.GM)),
    db: Session = Depends(get_db),
):
    """Reject a pending deal at any stage. Requires GM or higher."""
    try:
        return await deal_service.reject_deal(db, auth.org_id, req.deal_id, auth.role_name)
    except (DealValidationError, DealNotFoundError, ValueError) as e:
        _handle_deal_error(e)


# ── GET /deals ──────────────────────────────────────────────────────────────

@router.get("/deals", response_model=list[DealResponse])
def list_deals(
    status: str = Query(None, description="Filter: PENDING | APPROVED | REJECTED"),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List recent deals for the organization, optionally filtered by status."""
    branch_id = None if auth.sees_all_branches() else auth.branch_id
    return deal_service.get_deals(db, auth.org_id, status=status, branch_id=branch_id)


# ── GET /deals/deleted — MUST be before /deals/{deal_id} to avoid shadowing ──

@router.get("/deals/deleted", response_model=list[DealResponse])
def list_deleted_deals(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = 50,
):
    """List all soft-deleted deals (ADMIN only)."""
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="Only ADMIN can view deleted deals")
    return deal_service.get_deleted_deals(db, auth.org_id, limit)


# ── GET /deals/{deal_id} ────────────────────────────────────────────────────

@router.get("/deals/{deal_id}", response_model=DealDetailResponse)
def get_deal_detail(
    deal_id: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return full deal details with embedded audit event timeline."""
    deal = db.query(Deal).filter(
        Deal.id == deal_id, Deal.organization_id == auth.org_id, Deal.is_deleted == False
    ).first()
    if not deal:
        raise HTTPException(status_code=404, detail="Deal not found")

    events = (
        db.query(DealEvent)
        .filter(DealEvent.deal_id == deal_id)
        .order_by(DealEvent.created_at.asc())
        .all()
    )

    deal_data = DealResponse.model_validate(deal).model_dump()
    deal_data["events"] = [
        DealEventResponse.model_validate(e).model_dump() for e in events
    ]
    return deal_data


# ── GET /variants ───────────────────────────────────────────────────────────

@router.get("/variants")
def list_variants(
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return active variants for the deal form autocomplete dropdown."""
    return deal_service.get_variants(db, auth.org_id)


# ── SOFT DELETE / RESTORE / AUDIT (ADMIN ONLY) ───────────────────────────────

@router.delete("/deals/{deal_id}")
@limiter.limit("10/minute")
async def soft_delete_deal(
    request: Request,
    deal_id: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Soft-delete a deal (ADMIN only).
    Record is NEVER removed — recoverable via /deals/{id}/restore.
    """
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="Only ADMIN can delete deals")

    ip = request.client.host if request.client else None
    success = deal_service.soft_delete_deal(
        db, deal_id,
        org_id=auth.org_id,
        deleted_by=auth.user_id,
        deleted_by_email=auth.email,
        ip_address=ip,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Deal not found or already deleted")

    return {"success": True, "message": "Deal moved to trash — recoverable by admin"}


@router.post("/deals/{deal_id}/restore")
async def restore_deal(
    request: Request,
    deal_id: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Restore a soft-deleted deal (ADMIN only)."""
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="Only ADMIN can restore deals")

    ip = request.client.host if request.client else None
    success = deal_service.restore_deal(
        db, deal_id,
        org_id=auth.org_id,
        restored_by=auth.user_id,
        restored_by_email=auth.email,
        ip_address=ip,
    )
    if not success:
        raise HTTPException(status_code=404, detail="Deal not found or not deleted")

    return {"success": True, "message": "Deal restored successfully"}


# ── GET /audit-logs ──────────────────────────────────────────────────────────

@router.get("/audit-logs", response_model=list[AuditLogResponse])
def list_audit_logs(
    entity_type: Optional[str] = Query(None, description="Filter: deal | user | pricing"),
    entity_id: Optional[str] = Query(None, description="Specific entity ID"),
    action_type: Optional[str] = Query(None, description="Filter: DELETE | RESTORE | APPROVE | REJECT | CREATE"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Full audit trail for the organization (ADMIN only).
    Every DELETE, RESTORE, CREATE, APPROVE, REJECT action is logged here.
    """
    if not auth.is_admin():
        raise HTTPException(status_code=403, detail="Only ADMIN can view audit logs")

    logs = get_audit_logs(
        db,
        org_id=auth.org_id,
        entity_type=entity_type,
        entity_id=entity_id,
        action_type=action_type,
        limit=limit,
        offset=offset,
    )
    return [AuditLogResponse.model_validate(l) for l in logs]


