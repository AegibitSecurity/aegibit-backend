"""
Phone Verification & Fraud Prevention Routes

Endpoints:
  GET  /check-customer?phone=XXXXXXXXXX  → Check customer status and deal history
  POST /send-otp                       → Send OTP to phone
  POST /verify-otp                     → Verify OTP and create/update customer
  GET  /fraud-logs                     → Get fraud audit logs (ADMIN only)
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

from app.database import get_db
from app.auth import AuthContext, get_current_user, require_admin
from app.services import verification_service as vs
from app.schemas import (
    SendOtpRequest,
    SendOtpResponse,
    VerifyOtpRequest,
    VerifyOtpResponse,
    CheckCustomerResponse,
    FraudAuditEntry,
)

router = APIRouter(tags=["verification"])


# ── Check Customer ─────────────────────────────────────────────────────────────

@router.get("/check-customer", response_model=CheckCustomerResponse)
def check_customer(
    phone: str = Query(..., description="10-digit phone number", min_length=10, max_length=15),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Check if a customer exists by phone number.
    Returns customer details, deal count, and recent deals.
    Shows warning if customer has multiple deals (potential fraud).
    """
    # Normalize phone number
    cleaned_phone = ''.join(c for c in phone if c.isdigit())
    if len(cleaned_phone) > 10:
        cleaned_phone = cleaned_phone[-10:]

    if len(cleaned_phone) != 10 or cleaned_phone[0] not in '6789':
        raise HTTPException(status_code=400, detail="Invalid phone number format")

    return vs.check_customer(db, cleaned_phone, auth.org_id)


# ── Send OTP ─────────────────────────────────────────────────────────────────

@router.post("/send-otp", response_model=SendOtpResponse)
def send_otp(
    body: SendOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Send OTP to phone number.
    Rate limited: max 1 OTP per 30 seconds per phone.
    OTP expires in 5 minutes.
    """
    try:
        return vs.send_otp(db, body.phone_number, auth.org_id, auth.user_id)
    except ValueError as e:
        raise HTTPException(status_code=429, detail=str(e))


# ── Verify OTP ───────────────────────────────────────────────────────────────

@router.post("/verify-otp", response_model=VerifyOtpResponse)
def verify_otp(
    body: VerifyOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Verify OTP and create/update customer record.
    Max 3 attempts per OTP.
    On success, marks customer as verified.
    """
    try:
        return vs.verify_otp(
            db, body.phone_number, body.otp,
            auth.org_id, auth.user_id
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ── Fraud Logs (ADMIN only) ──────────────────────────────────────────────────

@router.get("/fraud-logs", response_model=list[FraudAuditEntry])
def get_fraud_logs(
    phone: Optional[str] = Query(None, description="Filter by phone number"),
    salesperson_id: Optional[str] = Query(None, description="Filter by salesperson"),
    limit: int = Query(50, ge=1, le=200),
    auth: AuthContext = Depends(require_admin()),
    db: Session = Depends(get_db),
):
    """
    Get fraud audit logs. ADMIN only.
    Shows all fraud detection events, OTP attempts, and blocked deals.
    """
    logs = vs.get_fraud_logs(db, auth.org_id, phone, salesperson_id, limit)
    return logs


# ── Test OTP (Development Only) ──────────────────────────────────────────────

@router.get("/debug-otp", include_in_schema=False)
def debug_otp(
    phone: str = Query(...),
    db: Session = Depends(get_db),
):
    """
    DEBUG ONLY: Get the current OTP for a phone number.
    This endpoint should be disabled in production.
    """
    # In production, this should check for debug mode
    verification = (
        db.query(vs.PhoneVerification)
        .filter(
            vs.PhoneVerification.phone_number == phone,
            vs.PhoneVerification.verified == False
        )
        .order_by(vs.PhoneVerification.created_at.desc())
        .first()
    )

    if not verification:
        return {"error": "No active OTP found"}

    return {
        "phone": phone,
        "otp": verification.otp,
        "expires_at": verification.expires_at,
        "attempts": verification.attempts,
        "remaining_attempts": verification.max_attempts - verification.attempts,
    }
