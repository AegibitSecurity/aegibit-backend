"""
Email OTP Service — Business logic for phone verification via email OTP

Features:
  - Generate 6-digit OTP
  - Hash OTP with bcrypt (never store plain text)
  - Rate limiting: max 3 OTPs per 10 minutes per email/phone
  - 5-minute OTP expiry
  - 3 max verification attempts
  - Check phone uniqueness across deals
"""

import random
import bcrypt
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models import OtpVerification, Deal
from app.services.email_service import send_otp_email, mask_email

# ── Configuration ───────────────────────────────────────────────────────────

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
RATE_LIMIT_MINUTES = 10
MAX_OTP_PER_WINDOW = 3  # Max 3 OTPs per rate limit window
MAX_VERIFY_ATTEMPTS = 3


# ── OTP Generation & Hashing ─────────────────────────────────────────────────

def _generate_otp() -> str:
    """Generate a 6-digit OTP."""
    return ''.join(random.choices('0123456789', k=OTP_LENGTH))


def _hash_otp(otp: str) -> str:
    """Hash OTP using bcrypt."""
    return bcrypt.hashpw(otp.encode('utf-8'), bcrypt.gensalt(rounds=10)).decode('utf-8')


def _verify_otp_hash(otp: str, hashed: str) -> bool:
    """Verify OTP against hash."""
    return bcrypt.checkpw(otp.encode('utf-8'), hashed.encode('utf-8'))


# ── Rate Limiting ────────────────────────────────────────────────────────────

def _check_rate_limit(db: Session, email: str, phone: str) -> Tuple[bool, int]:
    """
    Check if user has exceeded OTP rate limit.
    Returns (allowed, seconds_until_reset).
    """
    window_start = datetime.now(timezone.utc) - timedelta(minutes=RATE_LIMIT_MINUTES)

    # Count recent OTPs for this email OR phone
    recent_count = (
        db.query(OtpVerification)
        .filter(
            ((OtpVerification.email == email) | (OtpVerification.phone == phone)) &
            (OtpVerification.created_at >= window_start)
        )
        .count()
    )

    if recent_count >= MAX_OTP_PER_WINDOW:
        # Find the oldest OTP in the window to calculate reset time
        oldest = (
            db.query(OtpVerification)
            .filter(
                ((OtpVerification.email == email) | (OtpVerification.phone == phone)) &
                (OtpVerification.created_at >= window_start)
            )
            .order_by(OtpVerification.created_at.asc())
            .first()
        )
        if oldest:
            reset_time = oldest.created_at + timedelta(minutes=RATE_LIMIT_MINUTES)
            seconds_remaining = int((reset_time - datetime.now(timezone.utc)).total_seconds())
            return False, max(0, seconds_remaining)
        return False, RATE_LIMIT_MINUTES * 60

    return True, 0


# ── Phone Uniqueness Check ───────────────────────────────────────────────────

def check_phone_has_deal(db: Session, phone: str) -> bool:
    """
    Check if phone number already exists in any non-deleted deal.
    This enforces the unique phone constraint.
    """
    exists = (
        db.query(Deal)
        .filter(Deal.customer_phone == phone, Deal.is_deleted == False)
        .first()
    )
    return exists is not None


def get_existing_deal_for_phone(db: Session, phone: str) -> Optional[Deal]:
    """Get existing non-deleted deal for a phone number (for error messages)."""
    return (
        db.query(Deal)
        .filter(Deal.customer_phone == phone, Deal.is_deleted == False)
        .first()
    )


# ── Public API ──────────────────────────────────────────────────────────────

def send_email_otp(db: Session, email: str, phone: str) -> dict:
    """
    Send OTP to email for phone verification.

    Returns:
        {
            "success": bool,
            "message": str,
            "email_masked": str,
            "expires_in_seconds": int,
            "rate_limit_seconds": int (if rate limited)
        }

    Raises:
        ValueError: If phone already has a deal or rate limited
    """
    # 1. Check if phone already exists in deals
    if check_phone_has_deal(db, phone):
        existing_deal = get_existing_deal_for_phone(db, phone)
        raise ValueError(
            f"Phone number {phone} is already registered to a deal. "
            f"Each phone number can only be used for one deal. "
            f"Contact support if you need to update the existing deal."
        )

    # 2. Check rate limiting
    allowed, seconds_remaining = _check_rate_limit(db, email, phone)
    if not allowed:
        raise ValueError(
            f"Too many OTP requests. Please try again in {seconds_remaining // 60} minutes."
        )

    # 3. Generate and hash OTP
    otp = _generate_otp()
    otp_hash = _hash_otp(otp)

    # 4. Calculate expiry
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)

    # 5. Store in database
    otp_record = OtpVerification(
        email=email.lower().strip(),
        phone=phone,
        otp_hash=otp_hash,
        expires_at=expires_at,
        verified=False,
        attempts=0,
        max_attempts=MAX_VERIFY_ATTEMPTS,
    )
    db.add(otp_record)
    db.commit()

    # 6. Send email
    email_sent = send_otp_email(email, otp, phone)

    if not email_sent:
        # Don't expose OTP if email fails, but keep record for retry
        raise ValueError("Failed to send OTP email. Please try again.")

    return {
        "success": True,
        "message": f"OTP sent to {mask_email(email)}",
        "email_masked": mask_email(email),
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


def verify_email_otp(db: Session, email: str, phone: str, otp: str) -> dict:
    """
    Verify OTP for phone verification.

    Returns:
        {
            "success": bool,
            "verified": bool,
            "message": str,
            "phone": str,
            "remaining_attempts": int (if failed)
        }

    Raises:
        ValueError: If OTP invalid, expired, or max attempts exceeded
    """
    # 1. Find the most recent unverified OTP for this email+phone
    otp_record = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.email == email.lower().strip(),
            OtpVerification.phone == phone,
            OtpVerification.verified == False
        )
        .order_by(OtpVerification.created_at.desc())
        .first()
    )

    if not otp_record:
        raise ValueError("No active OTP found. Please request a new OTP.")

    # 2. Check expiry
    now = datetime.now(timezone.utc)
    if otp_record.expires_at < now:
        raise ValueError("OTP has expired. Please request a new OTP.")

    # 3. Check max attempts
    if otp_record.attempts >= otp_record.max_attempts:
        raise ValueError(
            f"Maximum attempts ({otp_record.max_attempts}) exceeded. "
            "Please request a new OTP."
        )

    # 4. Verify OTP hash
    if not _verify_otp_hash(otp, otp_record.otp_hash):
        # Increment attempt counter
        otp_record.attempts += 1
        db.commit()

        remaining = otp_record.max_attempts - otp_record.attempts
        raise ValueError(
            f"Invalid OTP. {remaining} attempt{'s' if remaining != 1 else ''} remaining."
        )

    # 5. Mark as legitimately verified
    otp_record.verified = True
    otp_record.legitimately_verified = True
    otp_record.verified_at = now
    db.commit()

    return {
        "success": True,
        "verified": True,
        "message": "Phone number verified successfully",
        "phone": phone,
    }


def check_phone_status(db: Session, phone: str) -> dict:
    """
    Check status of a phone number.
    Returns info about existing deals and verification status.
    """
    has_deal = check_phone_has_deal(db, phone)

    # Check if there's a verified OTP for this phone
    verified_otp = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.phone == phone,
            OtpVerification.verified == True
        )
        .order_by(OtpVerification.verified_at.desc())
        .first()
    )

    message = None
    if has_deal:
        existing_deal = get_existing_deal_for_phone(db, phone)
        message = (
            f"This phone number is already associated with a deal "
            f"(created {existing_deal.created_at.strftime('%Y-%m-%d')}). "
            f"Each phone can only be used for one deal."
        )

    return {
        "phone": phone,
        "exists": has_deal or verified_otp is not None,
        "has_deal": has_deal,
        "verified": verified_otp is not None,
        "message": message,
    }


def is_phone_verified_for_deal(db: Session, phone: str) -> bool:
    """
    Check if phone was legitimately verified (user entered correct OTP).
    Uses legitimately_verified to exclude invalidated OTP records.
    """
    verified = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.phone == phone,
            OtpVerification.legitimately_verified == True
        )
        .first()
    )
    return verified is not None
