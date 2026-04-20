"""
Deal Service — CRUD operations and workflow orchestration for deals.

Pipeline:  validate → lookup pricing → Profit Guard → persist → route tasks → notify

All pricing comes from the DB (CarModel). No manual price input is accepted.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session
from thefuzz import fuzz

from app.models import Deal, DealEvent, CarModel, OrgConfig, Task, Customer
from app.services.audit_service import log_action, Action, Entity
from app.schemas import (
    DealCreateRequest,
    DealAnalyzeRequest,
    DealAnalyzeResponse,
    DealResponse,
)
from app.services import verification_service as vs
from app.services import email_otp_service as email_otp
from app.services import otp_service_secure as otp_secure
from app.services.profit_guard import (
    evaluate as profit_guard_evaluate,
    AUTO_APPROVE,
    GM_APPROVAL,
    DIRECTOR_APPROVAL,
)
from app.services.task_service import create_task, complete_task
from app.services.notification_service import (
    notify_deal_created,
    notify_gm_approved,
    notify_gm_escalated,
    notify_director_approved,
    notify_deal_rejected,
    notify_task_created,
    notify_task_completed,
)
from app.services.pricing_engine import (
    calculate_final_price,
    PricingInput,
    PricingBreakdown,
    PricingError,
)

logger = logging.getLogger(__name__)

# Accepted registration types → (CarModel field, human label)
VALID_REG_TYPES: dict[str, tuple[str, str]] = {
    "5YR":        ("total_5yr",  "5-Year Registration"),
    "15YR":       ("total_15yr", "15-Year Registration"),
    "BH":         ("total_bh",   "BH Registration"),
    "INDIVIDUAL": ("total_5yr",  "Individual (default 5-Year)"),
}


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

class DealValidationError(Exception):
    """Raised for invalid deal inputs (400)."""


class DealNotFoundError(Exception):
    """Raised when a referenced entity doesn't exist (404)."""


def _get_pricing(db: Session, org_id: str, variant: str) -> Optional[CarModel]:
    """
    Find the active pricing row for a given variant.
    Exact match on normalized (lowercase, stripped) variant name.
    """
    variant_lower = variant.strip().lower()
    return (
        db.query(CarModel)
        .filter(
            CarModel.organization_id == org_id,
            CarModel.variant == variant_lower,
            CarModel.is_active == True,
        )
        .first()
    )


def _fuzzy_find_variant(db: Session, org_id: str, variant: str) -> Optional[CarModel]:
    """
    If exact match fails, do a fuzzy search across active variants.
    Returns the best match above 80% similarity, or None.
    """
    all_active = (
        db.query(CarModel)
        .filter(
            CarModel.organization_id == org_id,
            CarModel.is_active == True,
        )
        .all()
    )
    if not all_active:
        return None

    input_lower = variant.strip().lower()
    best_score = 0
    best_match = None

    for car in all_active:
        score = fuzz.token_set_ratio(input_lower, car.variant)
        if score > best_score:
            best_score = score
            best_match = car

    if best_score >= 80 and best_match is not None:
        logger.info(
            "Fuzzy matched variant '%s' → '%s' (score=%d)",
            variant, best_match.variant, best_score,
        )
        return best_match

    return None


def _resolve_pricing(db: Session, org_id: str, variant: str) -> CarModel:
    """
    Look up pricing: exact match first, then fuzzy fallback.
    Raises DealNotFoundError if nothing found.
    """
    pricing = _get_pricing(db, org_id, variant)
    if pricing:
        return pricing

    # Fuzzy fallback
    pricing = _fuzzy_find_variant(db, org_id, variant)
    if pricing:
        return pricing

    # Build a helpful error with available variants
    available = (
        db.query(CarModel.variant)
        .filter(CarModel.organization_id == org_id, CarModel.is_active == True)
        .order_by(CarModel.variant)
        .limit(10)
        .all()
    )
    variant_list = [v[0] for v in available]
    raise DealNotFoundError(
        f"No active pricing found for variant '{variant}'. "
        f"Available variants: {variant_list or '(none — upload pricing first)'}"
    )


def _resolve_base_price(pricing: CarModel, reg_type: str) -> float:
    """Pick the correct price column based on registration type."""
    reg = reg_type.upper()
    field_name, _ = VALID_REG_TYPES.get(reg, ("total_5yr", "default"))

    value = getattr(pricing, field_name, None)
    if value is not None:
        return float(value)

    # Fallback chain: try total_5yr → ex_showroom
    if pricing.total_5yr is not None:
        return float(pricing.total_5yr)
    return float(pricing.ex_showroom_price)


def _get_org_config(db: Session, org_id: str) -> OrgConfig:
    """Fetch org config or raise."""
    config = db.query(OrgConfig).filter(OrgConfig.organization_id == org_id).first()
    if not config:
        raise DealNotFoundError(
            "Organization configuration not found. Contact your administrator."
        )
    return config


def _validate_deal_inputs(req: DealCreateRequest):
    """
    Pre-flight validation on deal creation inputs.
    Raises DealValidationError on bad data.
    """
    # Customer name
    if not req.customer_name or not req.customer_name.strip():
        raise DealValidationError("Customer name is required.")

    # Variant
    if not req.variant or not req.variant.strip():
        raise DealValidationError("Vehicle variant is required.")

    # Discount
    if req.discount < 0:
        raise DealValidationError(
            f"Discount cannot be negative (got ₹{req.discount:,.0f})."
        )

    # Registration type
    reg = req.registration_type.upper()
    if reg not in VALID_REG_TYPES:
        valid_keys = sorted(VALID_REG_TYPES.keys())
        raise DealValidationError(
            f"Invalid registration type '{req.registration_type}'. "
            f"Must be one of: {valid_keys}"
        )

    # ── Customer detail validation (only when detail fields are provided) ────
    _validate_customer_details(req)


def _validate_customer_details(req: DealCreateRequest):
    """
    Validate customer detail fields when they are provided.
    Skips validation entirely if no detail fields are present (backward compat).
    """
    has_details = any(
        getattr(req, f, None) is not None
        for f in ("address", "aadhaar", "pan", "voter_id", "rse_name", "sm_name")
    )
    if not has_details:
        return  # legacy request — skip all customer detail validation

    # Phone format: must be numeric if provided
    if req.phone:
        cleaned = req.phone.strip().replace("+", "").replace("-", "").replace(" ", "")
        if not cleaned.isdigit():
            raise DealValidationError(
                f"Phone number must be numeric (got '{req.phone}')."
            )

    # At least one ID document required
    id_docs = [req.aadhaar, req.pan, req.voter_id]
    if not any(doc and doc.strip() for doc in id_docs):
        raise DealValidationError(
            "At least one ID document is required: aadhaar, pan, or voter_id."
        )


def _has_pricing_fields(req: DealCreateRequest) -> bool:
    """
    Check if the request contains any pricing engine fields.

    If ANY of [road_tax, insurance, corporate_discount, festival_discount,
    adjustments] is provided (not None), use the full pricing engine.
    Otherwise fall back to the legacy simple formula.
    """
    return any(
        getattr(req, f, None) is not None
        for f in ("road_tax", "insurance", "corporate_discount",
                  "festival_discount", "adjustments")
    )


def _map_decision_to_status(decision: str) -> tuple[str, str]:
    """
    Map a Profit Guard decision to (deal status, approval_stage).

    AUTO_APPROVE      → (APPROVED, DONE)
    GM_APPROVAL       → (PENDING,  GM)
    DIRECTOR_APPROVAL → (PENDING,  DIRECTOR)
    """
    if decision == AUTO_APPROVE:
        return "APPROVED", "DONE"
    elif decision == GM_APPROVAL:
        return "PENDING", "GM"
    else:  # DIRECTOR_APPROVAL
        return "PENDING", "DIRECTOR"


def _log_event(
    db: Session,
    deal_id: str,
    event_type: str,
    actor_role: str = None,
    metadata: dict = None,
):
    """Write an immutable audit event."""
    db.add(DealEvent(
        deal_id=deal_id,
        event_type=event_type,
        actor_role=actor_role,
        metadata_=metadata,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# Analyze (read-only preview, no DB writes)
# ─────────────────────────────────────────────────────────────────────────────

def analyze_deal(
    db: Session,
    org_id: str,
    req: DealAnalyzeRequest,
) -> DealAnalyzeResponse:
    """Evaluate deal parameters without persisting anything."""
    pricing = _resolve_pricing(db, org_id, req.variant)
    org_config = _get_org_config(db, org_id)

    base_price = _resolve_base_price(pricing, req.registration_type)
    cost = float(pricing.ex_showroom_price)
    final_price = base_price - req.discount

    pg = profit_guard_evaluate(base_price, req.discount, cost, org_config)

    return DealAnalyzeResponse(
        variant=pricing.variant,  # return the DB-canonical variant name
        base_price=base_price,
        discount=req.discount,
        final_price=final_price,
        margin=pg.margin,
        margin_percent=pg.margin_percent,
        risk_level=pg.risk_level,
        decision=pg.decision,
        reason=pg.reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Create Deal  (the main pipeline)
# ─────────────────────────────────────────────────────────────────────────────

async def create_deal(
    db: Session,
    org_id: str,
    req: DealCreateRequest,
    salesperson_id: str = None,
    skip_verification: bool = False,  # For testing/legacy
) -> DealResponse:
    """
    Full deal creation pipeline:
      1. Validate inputs
      2. Lookup pricing from active DB batch (no manual prices)
      3. Resolve base_price by registration type
      4. Run Profit Guard engine
      5. Persist deal with status + approval_stage
      6. Auto-create approval task when not auto-approved
      7. Write audit event
      8. Fire notification + WebSocket broadcast

    Returns the created DealResponse.
    """

    # ── 1. Validate inputs ────────────────────────────────────────────────────
    _validate_deal_inputs(req)

    # ── 1b. Phone Verification via Email OTP (SECURE) ───────────────────────────
    # Extract phone from mobile field (primary) or phone field
    phone = req.mobile.strip() if req.mobile else (req.phone.strip() if req.phone else None)
    email = req.customer_email.strip() if hasattr(req, 'customer_email') and req.customer_email else None

    if phone and not skip_verification:
        # Check 1: Phone must be unique (not already in a deal)
        phone_exists, existing_deal = otp_secure.check_phone_exists_in_deal(db, phone)
        if phone_exists:
            raise DealValidationError(
                "Phone number already used for another customer. "
                "Each phone can only be used for one deal."
            )

        # Check 2: Phone must be verified via Email OTP
        if not otp_secure.is_phone_verified_for_deal(db, phone):
            raise DealValidationError(
                "Phone number is not verified. Please verify via email OTP before creating a deal."
            )

        # Check 3: Fraud rules
        fraud_check = vs.check_fraud_rules(db, phone, org_id, salesperson_id)
        if not fraud_check.allowed:
            raise DealValidationError(fraud_check.reason)

        customer_id = None  # Email OTP doesn't use the Customer table
    else:
        customer_id = None

    # ── 2. Fetch pricing from DB — variant must exist in active batch ─────────
    pricing = _resolve_pricing(db, org_id, req.variant)

    # ── 3. Fetch org config (thresholds) ──────────────────────────────────────
    org_config = _get_org_config(db, org_id)

    # ── 4. Resolve prices ─────────────────────────────────────────────────────
    cost = float(pricing.ex_showroom_price)  # cost proxy for margin
    breakdown_dict = None

    # Always use pricing engine for accurate calculations including TCS
    try:
        pb = calculate_final_price(PricingInput(
            ex_showroom=cost,
            road_tax=req.road_tax or 0.0,
            insurance=req.insurance or 0.0,
            corporate_discount=req.corporate_discount or 0.0,
            festival_discount=req.festival_discount or 0.0,
            manual_discount=req.discount,
            adjustments=req.adjustments or 0.0,
        ))
    except PricingError as e:
        raise DealValidationError(str(e))

    final_price = pb.final_price
    base_price = pb.ex_showroom
    discount_for_pg = pb.total_discount
    breakdown_dict = pb.to_dict()

    # Sanity check: final price should not be zero or negative from a
    # data perspective (Profit Guard handles the margin evaluation separately)
    if final_price <= 0:
        raise DealValidationError(
            f"Final price would be ₹{final_price:,.0f} "
            f"(base ₹{base_price:,.0f} - discount ₹{discount_for_pg:,.0f}). "
            f"Discount cannot exceed the base price."
        )

    # ── 5. Run Profit Guard ───────────────────────────────────────────────────
    pg = profit_guard_evaluate(base_price, discount_for_pg, cost, org_config)

    # ── 6. Map decision → status + approval stage ────────────────────────────
    status, approval_stage = _map_decision_to_status(pg.decision)

    logger.info(
        "Deal for '%s' by '%s': decision=%s health=%s margin=%.0f (%.1f%%)",
        pricing.variant, req.customer_name,
        pg.decision, pg.deal_health, pg.margin, pg.margin_percent,
    )

    # ── 7. Persist deal ───────────────────────────────────────────────────────
    deal = Deal(
        organization_id=org_id,
        customer_id=customer_id,
        salesperson_id=salesperson_id,
        customer_name=req.customer_name.strip(),
        customer_phone=phone,  # UNIQUE constraint enforced
        customer_email=email,   # For OTP delivery record
        phone_verified=True,  # Verified via email OTP
        phone=req.phone.strip() if req.phone else None,
        mobile=req.mobile.strip() if req.mobile else None,
        father_name=req.father_name.strip() if req.father_name else None,
        address=req.address.strip() if req.address else None,
        aadhaar=req.aadhaar.strip() if req.aadhaar else None,
        pan=req.pan.strip().upper() if req.pan else None,
        voter_id=req.voter_id.strip() if req.voter_id else None,
        rse_name=req.rse_name.strip() if req.rse_name else None,
        sm_name=req.sm_name.strip() if req.sm_name else None,
        delivery_date=req.delivery_date,
        crm_date=req.crm_date,
        crm_invoice_no=req.crm_invoice_no.strip() if req.crm_invoice_no else None,
        crm_esp=req.crm_esp,
        variant=pricing.variant,  # use DB-canonical name, not user input
        model=req.model.strip(),  # required field
        colour=req.colour.strip() if req.colour else None,
        chassis_no=req.chassis_no.strip() if req.chassis_no else None,
        engine_no=req.engine_no.strip() if req.engine_no else None,
        registration_type=req.registration_type.upper(),
        sale_type=req.sale_type.upper(),
        financer_name=req.financer_name.strip() if req.financer_name else None,
        financer_branch=req.financer_branch.strip() if req.financer_branch else None,
        inhouse_finance=req.inhouse_finance.upper(),
        base_price=base_price,
        discount=discount_for_pg,
        final_price=final_price,
        margin=pg.margin,
        margin_percent=pg.margin_percent,
        status=status,
        approval_stage=approval_stage,
        risk_level=pg.risk_level,
        decision=pg.decision,
        reason=pg.reason,
        pricing_breakdown=breakdown_dict,
    )
    db.add(deal)
    db.flush()  # get deal.id before creating task

    # ── 8. Audit log ─────────────────────────────────────────────────────────
    _log_event(db, deal.id, "DEAL_CREATED", metadata={
        "decision": pg.decision,
        "deal_health": pg.deal_health,
        "risk": pg.risk_level,
        "margin": pg.margin,
        "margin_percent": pg.margin_percent,
        "base_price": base_price,
        "cost": cost,
        "discount": req.discount,
        "final_price": final_price,
    })

    # ── 9. Auto-create approval task ──────────────────────────────────────────
    if pg.decision != AUTO_APPROVE:
        task = create_task(db, deal.id, approval_stage)
        logger.info(
            "Created %s approval task %s for deal %s",
            approval_stage, task.id, deal.id,
        )
        # Task notification is fired after commit (below)

    # ── 10. Commit everything ─────────────────────────────────────────────────
    db.commit()
    db.refresh(deal)

    # ── 11. Notify (DB + WebSocket in one call) ────────────────────────────────
    await notify_deal_created(db, org_id, _deal_to_dict(deal), pg.decision)

    # ── 12. Notify task creation ──────────────────────────────────────────────
    if pg.decision != AUTO_APPROVE:
        await notify_task_created(db, org_id, deal.id, approval_stage)

    return DealResponse.model_validate(deal)


# ─────────────────────────────────────────────────────────────────────────────
# Approvals
# ─────────────────────────────────────────────────────────────────────────────

async def approve_gm(db: Session, org_id: str, deal_id: str) -> DealResponse:
    """GM approves a deal. If discount exceeds director limit, escalate."""
    deal = db.query(Deal).filter(
        Deal.id == deal_id, Deal.organization_id == org_id, Deal.is_deleted == False
    ).first()
    if not deal:
        raise DealNotFoundError("Deal not found")

    if deal.status != "PENDING":
        raise DealValidationError(f"Deal is already {deal.status}, cannot approve.")

    org_config = _get_org_config(db, org_id)

    # Check if needs director escalation
    if deal.discount > org_config.director_discount_limit:
        deal.approval_stage = "DIRECTOR"
        complete_task(db, deal.id, "GM")
        create_task(db, deal.id, "DIRECTOR")
        _log_event(db, deal.id, "ESCALATED_TO_DIRECTOR", actor_role="GM")
        db.commit()

        await notify_gm_escalated(db, org_id, _deal_to_dict(deal))
        await notify_task_completed(db, org_id, deal.id, "GM")
        await notify_task_created(db, org_id, deal.id, "DIRECTOR")
    else:
        deal.status = "APPROVED"
        deal.approval_stage = "DONE"
        complete_task(db, deal.id, "GM")
        _log_event(db, deal.id, "APPROVED_BY_GM", actor_role="GM")
        db.commit()

        await notify_gm_approved(db, org_id, _deal_to_dict(deal))
        await notify_task_completed(db, org_id, deal.id, "GM")

    db.refresh(deal)
    return DealResponse.model_validate(deal)


async def approve_director(db: Session, org_id: str, deal_id: str) -> DealResponse:
    """Director approves a deal — final authority."""
    deal = db.query(Deal).filter(Deal.id == deal_id, Deal.organization_id == org_id, Deal.is_deleted == False).first()
    if not deal:
        raise DealNotFoundError("Deal not found")

    if deal.status != "PENDING":
        raise DealValidationError("Deal is not in PENDING status")

    # Use customer_phone (the verified phone) or fall back to mobile/phone fields
    phone_for_fraud = deal.customer_phone or deal.mobile or deal.phone
    if phone_for_fraud and deal.salesperson_id:
        fraud_check = vs.check_fraud_rules(db, phone_for_fraud, org_id, deal.salesperson_id)
        if not fraud_check.allowed:
            raise DealValidationError(fraud_check.reason)

    deal.status = "APPROVED"
    deal.approval_stage = "DONE"
    complete_task(db, deal.id, "DIRECTOR")
    _log_event(db, deal.id, "APPROVED_BY_DIRECTOR", actor_role="DIRECTOR")
    db.commit()

    await notify_director_approved(db, org_id, _deal_to_dict(deal))
    await notify_task_completed(db, org_id, deal.id, "DIRECTOR")

    db.refresh(deal)
    return DealResponse.model_validate(deal)


async def reject_deal(db: Session, org_id: str, deal_id: str, actor_role: str = "GM") -> DealResponse:
    """Reject a deal at any stage."""
    deal = db.query(Deal).filter(
        Deal.id == deal_id, Deal.organization_id == org_id, Deal.is_deleted == False
    ).first()
    if not deal:
        raise DealNotFoundError("Deal not found")

    if deal.status != "PENDING":
        raise DealValidationError(f"Deal is already {deal.status}, cannot reject.")

    deal.status = "REJECTED"
    deal.approval_stage = "DONE"

    # Complete any pending tasks
    for task in db.query(Task).filter(Task.deal_id == deal.id, Task.status == "PENDING").all():
        task.status = "COMPLETED"
        task.completed_at = datetime.now(timezone.utc)

    _log_event(db, deal.id, "DEAL_REJECTED", actor_role=actor_role)
    db.commit()

    await notify_deal_rejected(db, org_id, _deal_to_dict(deal), actor_role)

    db.refresh(deal)
    return DealResponse.model_validate(deal)


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────

def get_deals(db: Session, org_id: str, status: str = None, limit: int = 50) -> list[DealResponse]:
    """Fetch non-deleted deals for an org, optionally filtered by status."""
    q = db.query(Deal).filter(Deal.organization_id == org_id, Deal.is_deleted == False)
    if status:
        q = q.filter(Deal.status == status.upper())
    deals = q.order_by(Deal.created_at.desc()).limit(limit).all()
    return [DealResponse.model_validate(d) for d in deals]


def get_deleted_deals(db: Session, org_id: str, limit: int = 50) -> list[DealResponse]:
    """Fetch soft-deleted deals for an org (ADMIN only)."""
    deals = (
        db.query(Deal)
        .filter(Deal.organization_id == org_id, Deal.is_deleted == True)
        .order_by(Deal.deleted_at.desc())
        .limit(limit)
        .all()
    )
    return [DealResponse.model_validate(d) for d in deals]


def soft_delete_deal(
    db: Session,
    deal_id: str,
    org_id: str,
    deleted_by: str,
    deleted_by_email: str = None,
    ip_address: str = None,
) -> bool:
    """
    Soft-delete a deal.  Sets is_deleted=True — record is NEVER removed.
    Enforces org isolation: admins from other orgs cannot delete this deal.
    Writes both a DealEvent and an AuditLog entry in the same transaction.
    """
    deal = db.query(Deal).filter(
        Deal.id == deal_id,
        Deal.organization_id == org_id,   # ← org isolation (security fix)
        Deal.is_deleted == False,
    ).first()
    if not deal:
        return False

    previous = {
        "status": deal.status,
        "customer_name": deal.customer_name,
        "variant": deal.variant,
        "final_price": deal.final_price,
        "created_at": deal.created_at.isoformat() if deal.created_at else None,
    }

    deal.is_deleted = True
    deal.deleted_at = datetime.now(timezone.utc)
    deal.deleted_by = deleted_by

    _log_event(db, deal.id, "DEAL_DELETED", metadata={"deleted_by": deleted_by})
    log_action(
        db,
        org_id=org_id,
        action_type=Action.DELETE,
        entity_type=Entity.DEAL,
        entity_id=deal_id,
        performed_by=deleted_by,
        performed_by_email=deleted_by_email,
        ip_address=ip_address,
        previous_data=previous,
    )
    db.commit()
    return True


def restore_deal(
    db: Session,
    deal_id: str,
    org_id: str,
    restored_by: str,
    restored_by_email: str = None,
    ip_address: str = None,
) -> bool:
    """
    Restore a soft-deleted deal.
    Enforces org isolation and writes full audit trail.
    """
    deal = db.query(Deal).filter(
        Deal.id == deal_id,
        Deal.organization_id == org_id,   # ← org isolation (security fix)
        Deal.is_deleted == True,
    ).first()
    if not deal:
        return False

    deal.is_deleted = False
    deal.deleted_at = None
    deal.deleted_by = None

    _log_event(db, deal.id, "DEAL_RESTORED", metadata={"restored_by": restored_by})
    log_action(
        db,
        org_id=org_id,
        action_type=Action.RESTORE,
        entity_type=Entity.DEAL,
        entity_id=deal_id,
        performed_by=restored_by,
        performed_by_email=restored_by_email,
        ip_address=ip_address,
        note=f"Restored by {restored_by_email or restored_by}",
    )
    db.commit()
    return True


def get_variants(db: Session, org_id: str):
    """Return active variants for autocomplete."""
    cars = (
        db.query(CarModel)
        .filter(CarModel.organization_id == org_id, CarModel.is_active == True)
        .order_by(CarModel.variant)
        .all()
    )
    return [
        {
            "variant": c.variant,
            "ex_showroom_price": c.ex_showroom_price,
            "total_5yr": c.total_5yr,
            "total_15yr": c.total_15yr,
            "total_bh": c.total_bh,
        }
        for c in cars
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Serialization helper
# ─────────────────────────────────────────────────────────────────────────────

def _deal_to_dict(deal: Deal) -> dict:
    return {
        "id": deal.id,
        "organization_id": deal.organization_id,
        "customer_name": deal.customer_name,
        "phone": deal.phone,
        "variant": deal.variant,
        "registration_type": deal.registration_type,
        "base_price": deal.base_price,
        "discount": deal.discount,
        "final_price": deal.final_price,
        "margin": deal.margin,
        "margin_percent": deal.margin_percent,
        "status": deal.status,
        "approval_stage": deal.approval_stage,
        "risk_level": deal.risk_level,
        "decision": deal.decision,
        "reason": deal.reason,
        "created_at": deal.created_at.isoformat() if deal.created_at else None,
    }
