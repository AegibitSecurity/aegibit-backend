"""
Phone Verification Service — OTP generation, fraud detection, rate limiting.

Provides:
  generate_otp()              — Create 6-digit OTP
  send_otp()                — Store OTP with rate limiting
  verify_otp()              — Validate OTP with attempt tracking
  check_customer()          — Get customer info and deal history
  check_fraud_rules()       — Validate deal creation limits
  mask_phone()              — Mask phone for UI display
  log_fraud_event()         — Audit logging

Security:
  - OTP expiry: 5 minutes
  - Max attempts: 3
  - Rate limit: 1 OTP per 30 seconds per phone
  - Fraud rules: max 3 deals/phone/day, max 10 deals/salesperson/hour
"""

import random
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from app.models import Customer, PhoneVerification, Deal, FraudAuditLog, User
from app.schemas import (
    CheckCustomerResponse,
    RecentDealSummary,
    FraudCheckResult,
    SendOtpResponse,
    VerifyOtpResponse,
)


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
MAX_OTP_ATTEMPTS = 3
OTP_RATE_LIMIT_SECONDS = 30

MAX_DAILY_DEALS_PER_PHONE = 3
MAX_HOURLY_DEALS_PER_SALESPERSON = 10


# ─────────────────────────────────────────────────────────────────────────────
# OTP Generation
# ─────────────────────────────────────────────────────────────────────────────

def generate_otp(length: int = OTP_LENGTH) -> str:
    """Generate a random 6-digit OTP."""
    return ''.join(random.choices('0123456789', k=length))


def mask_phone(phone: str) -> str:
    """Mask phone number for display: 98****3210"""
    if len(phone) != 10:
        return phone
    return f"{phone[:2]}****{phone[-4:]}"


# ─────────────────────────────────────────────────────────────────────────────
# Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

def _is_rate_limited(db: Session, phone: str) -> Tuple[bool, int]:
    """
    Check if phone is rate limited for OTP.
    Returns (is_limited, seconds_remaining).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=OTP_RATE_LIMIT_SECONDS)

    recent = (
        db.query(PhoneVerification)
        .filter(
            PhoneVerification.phone_number == phone,
            PhoneVerification.created_at >= cutoff,
            PhoneVerification.verified == False
        )
        .order_by(PhoneVerification.created_at.desc())
        .first()
    )

    if recent:
        elapsed = (datetime.now(timezone.utc) - recent.created_at).total_seconds()
        remaining = max(0, OTP_RATE_LIMIT_SECONDS - int(elapsed))
        return True, remaining

    return False, 0


# ─────────────────────────────────────────────────────────────────────────────
# OTP Operations
# ─────────────────────────────────────────────────────────────────────────────

def send_otp(
    db: Session,
    phone: str,
    org_id: str,
    salesperson_id: str,
) -> SendOtpResponse:
    """
    Generate and store OTP with rate limiting.
    In production, this would integrate with MSG91 or Twilio.
    """
    # Check rate limiting
    is_limited, remaining = _is_rate_limited(db, phone)
    if is_limited:
        raise ValueError(
            f"Rate limit exceeded. Please wait {remaining} seconds before requesting another OTP."
        )

    # Invalidate any existing unverified OTPs for this phone
    db.query(PhoneVerification).filter(
        PhoneVerification.phone_number == phone,
        PhoneVerification.verified == False
    ).update({"verified": True})  # Mark as used to prevent confusion

    # Generate new OTP
    otp = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)

    # Store in database
    verification = PhoneVerification(
        phone_number=phone,
        otp=otp,
        expires_at=expires_at,
        attempts=0,
        max_attempts=MAX_OTP_ATTEMPTS,
        verified=False,
    )
    db.add(verification)

    # Log for audit
    _log_fraud_event(
        db, org_id, phone, salesperson_id, None,
        "OTP_SENT", {"expires_at": expires_at.isoformat()}
    )

    db.commit()

    # TODO: In production, send SMS via MSG91 or Twilio
    # For now, return OTP in response for testing (remove in production)
    print(f"[OTP] Generated for {mask_phone(phone)}: {otp}")

    return SendOtpResponse(
        message="OTP sent successfully",
        expires_in_seconds=OTP_EXPIRY_MINUTES * 60,
        masked_phone=mask_phone(phone),
    )


def verify_otp(
    db: Session,
    phone: str,
    otp: str,
    org_id: str,
    salesperson_id: str,
    customer_name: Optional[str] = None,
) -> VerifyOtpResponse:
    """
    Verify OTP and create/update customer record.
    Returns customer_id on success.
    """
    # Find the most recent unverified OTP for this phone
    verification = (
        db.query(PhoneVerification)
        .filter(
            PhoneVerification.phone_number == phone,
            PhoneVerification.verified == False
        )
        .order_by(PhoneVerification.created_at.desc())
        .first()
    )

    if not verification:
        _log_fraud_event(
            db, org_id, phone, salesperson_id, None,
            "OTP_VERIFY_FAILED", {"reason": "no_active_otp"}
        )
        db.commit()
        raise ValueError("No active OTP found. Please request a new OTP.")

    # Check expiry
    if datetime.now(timezone.utc) > verification.expires_at:
        _log_fraud_event(
            db, org_id, phone, salesperson_id, None,
            "OTP_VERIFY_FAILED", {"reason": "expired"}
        )
        db.commit()
        raise ValueError("OTP has expired. Please request a new OTP.")

    # Check attempts
    if verification.attempts >= verification.max_attempts:
        _log_fraud_event(
            db, org_id, phone, salesperson_id, None,
            "OTP_VERIFY_FAILED", {"reason": "max_attempts_exceeded"}
        )
        db.commit()
        raise ValueError("Maximum attempts exceeded. Please request a new OTP.")

    # Increment attempt counter
    verification.attempts += 1

    # Verify OTP
    if verification.otp != otp:
        db.commit()
        remaining = verification.max_attempts - verification.attempts
        raise ValueError(f"Invalid OTP. {remaining} attempts remaining.")

    # OTP verified successfully
    verification.verified = True

    # Create or update customer
    customer = db.query(Customer).filter(
        Customer.phone_number == phone,
        Customer.organization_id == org_id
    ).first()

    is_new_customer = False

    if customer:
        # Update existing customer
        customer.is_verified = True
        customer.verification_count += 1
        customer.verified_at = datetime.now(timezone.utc)
        if customer_name and not customer.name:
            customer.name = customer_name
    else:
        # Create new customer
        is_new_customer = True
        customer = Customer(
            organization_id=org_id,
            phone_number=phone,
            name=customer_name,
            is_verified=True,
            verification_count=1,
            verified_at=datetime.now(timezone.utc),
        )
        db.add(customer)

    db.flush()  # Get customer.id

    _log_fraud_event(
        db, org_id, phone, salesperson_id, None,
        "OTP_VERIFIED", {"customer_id": customer.id, "is_new": is_new_customer}
    )

    db.commit()

    return VerifyOtpResponse(
        verified=True,
        message="Phone number verified successfully",
        customer_id=customer.id,
        is_new_customer=is_new_customer,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Customer Check
# ─────────────────────────────────────────────────────────────────────────────

def check_customer(
    db: Session,
    phone: str,
    org_id: str,
) -> CheckCustomerResponse:
    """
    Check if customer exists and return deal history.
    Used during deal creation to show warnings.
    """
    customer = db.query(Customer).filter(
        Customer.phone_number == phone,
        Customer.organization_id == org_id
    ).first()

    if not customer:
        return CheckCustomerResponse(
            exists=False,
            is_verified=False,
            deal_count=0,
            recent_deals=[],
        )

    # Get recent non-deleted deals for this customer
    recent_deals = (
        db.query(Deal)
        .filter(
            Deal.customer_id == customer.id,
            Deal.organization_id == org_id,
            Deal.is_deleted == False
        )
        .order_by(Deal.created_at.desc())
        .limit(5)
        .all()
    )

    deal_count = (
        db.query(func.count(Deal.id))
        .filter(
            Deal.customer_id == customer.id,
            Deal.organization_id == org_id,
            Deal.is_deleted == False
        )
        .scalar() or 0
    )

    # Build warning message
    warning = None
    if deal_count >= 2:
        warning = f"This number already has {deal_count} deal(s). Please verify customer identity."

    return CheckCustomerResponse(
        exists=True,
        customer_id=customer.id,
        is_verified=customer.is_verified,
        name=customer.name,
        deal_count=deal_count,
        recent_deals=[
            RecentDealSummary(
                id=d.id,
                status=d.status,
                final_price=d.final_price,
                created_at=d.created_at,
                variant=d.variant,
            )
            for d in recent_deals
        ],
        warning_message=warning,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fraud Detection
# ─────────────────────────────────────────────────────────────────────────────

def check_fraud_rules(
    db: Session,
    phone: str,
    org_id: str,
    salesperson_id: str,
) -> FraudCheckResult:
    """
    Check fraud rules before allowing deal creation.
    Returns FraudCheckResult with allowed=True/False.
    """
    now = datetime.now(timezone.utc)

    # Count deals for this phone in last 24 hours
    day_ago = now - timedelta(days=1)
    daily_deals = (
        db.query(func.count(Deal.id))
        .filter(
            Deal.organization_id == org_id,
            or_(
                Deal.phone == phone,
                Deal.mobile == phone
            ),
            Deal.created_at >= day_ago
        )
        .scalar() or 0
    )

    # Count deals by this salesperson in last hour
    hour_ago = now - timedelta(hours=1)
    hourly_deals = (
        db.query(func.count(Deal.id))
        .filter(
            Deal.organization_id == org_id,
            Deal.salesperson_id == salesperson_id,
            Deal.created_at >= hour_ago
        )
        .scalar() or 0
    )

    # Check limits
    if daily_deals >= MAX_DAILY_DEALS_PER_PHONE:
        _log_fraud_event(
            db, org_id, phone, salesperson_id, None,
            "FRAUD_BLOCKED", {
                "reason": "daily_phone_limit_exceeded",
                "daily_deals": daily_deals,
                "limit": MAX_DAILY_DEALS_PER_PHONE
            }
        )
        db.commit()
        return FraudCheckResult(
            allowed=False,
            reason=f"Maximum {MAX_DAILY_DEALS_PER_PHONE} deals per phone number per day exceeded. This phone has {daily_deals} deals today.",
            daily_deals_for_phone=daily_deals,
            hourly_deals_for_salesperson=hourly_deals,
        )

    if hourly_deals >= MAX_HOURLY_DEALS_PER_SALESPERSON:
        _log_fraud_event(
            db, org_id, phone, salesperson_id, None,
            "FRAUD_BLOCKED", {
                "reason": "hourly_salesperson_limit_exceeded",
                "hourly_deals": hourly_deals,
                "limit": MAX_HOURLY_DEALS_PER_SALESPERSON
            }
        )
        db.commit()
        return FraudCheckResult(
            allowed=False,
            reason=f"Maximum {MAX_HOURLY_DEALS_PER_SALESPERSON} deals per hour exceeded for this salesperson. You have created {hourly_deals} deals in the last hour.",
            daily_deals_for_phone=daily_deals,
            hourly_deals_for_salesperson=hourly_deals,
        )

    return FraudCheckResult(
        allowed=True,
        daily_deals_for_phone=daily_deals,
        hourly_deals_for_salesperson=hourly_deals,
    )


def is_phone_verified(
    db: Session,
    phone: str,
    org_id: str,
) -> bool:
    """Check if a phone number is verified for this org."""
    customer = db.query(Customer).filter(
        Customer.phone_number == phone,
        Customer.organization_id == org_id,
        Customer.is_verified == True
    ).first()
    return customer is not None


def get_or_create_verified_customer(
    db: Session,
    phone: str,
    org_id: str,
    customer_name: Optional[str],
) -> Tuple[Customer, bool]:
    """
    Get existing verified customer or create new one.
    Returns (customer, is_new).
    """
    customer = db.query(Customer).filter(
        Customer.phone_number == phone,
        Customer.organization_id == org_id
    ).first()

    if customer:
        return customer, False

    # Create new unverified customer (will be verified via OTP)
    customer = Customer(
        organization_id=org_id,
        phone_number=phone,
        name=customer_name,
        is_verified=False,
        verification_count=0,
    )
    db.add(customer)
    db.flush()
    return customer, True


# ─────────────────────────────────────────────────────────────────────────────
# Audit Logging
# ─────────────────────────────────────────────────────────────────────────────

def _log_fraud_event(
    db: Session,
    org_id: str,
    phone: str,
    salesperson_id: str,
    deal_id: Optional[str],
    event_type: str,
    details: dict,
    ip_address: Optional[str] = None,
):
    """Write fraud audit log entry."""
    entry = FraudAuditLog(
        organization_id=org_id,
        phone_number=phone,
        salesperson_id=salesperson_id,
        deal_id=deal_id,
        event_type=event_type,
        details=details,
        ip_address=ip_address,
    )
    db.add(entry)


def get_fraud_logs(
    db: Session,
    org_id: str,
    phone: Optional[str] = None,
    salesperson_id: Optional[str] = None,
    limit: int = 50,
) -> list[FraudAuditLog]:
    """Get fraud audit logs for an org."""
    q = db.query(FraudAuditLog).filter(FraudAuditLog.organization_id == org_id)

    if phone:
        q = q.filter(FraudAuditLog.phone_number == phone)
    if salesperson_id:
        q = q.filter(FraudAuditLog.salesperson_id == salesperson_id)

    return q.order_by(FraudAuditLog.created_at.desc()).limit(limit).all()
