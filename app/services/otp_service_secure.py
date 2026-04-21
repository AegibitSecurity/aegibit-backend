"""
🔐 SECURE OTP Service — Hardened against spam, abuse, and brute-force attacks

Security Features:
  ✅ OTP via Resend API
  ✅ 6-digit numeric OTP
  ✅ 5-minute expiry
  ✅ bcrypt hashed OTP storage (NEVER raw OTP)
  ✅ Rate limiting: 3 OTPs per 10 min per phone/email
  ✅ Brute force: Max 5 verification attempts, then lock
  ✅ Duplicate protection: Phone already verified → BLOCK
  ✅ Resend control: 60-second cooldown, new OTP generated
  ✅ Replay attack protection: OTP invalidated after use
  ✅ Comprehensive security logging

Tech Stack:
  - FastAPI + SQLAlchemy
  - Resend API for email delivery
  - bcrypt for OTP hashing
  - SQLite/PostgreSQL
"""

import random
import bcrypt
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any

from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_

from app.models import OtpVerification, Deal
from app.services.email_service import send_otp_email, mask_email

logger = logging.getLogger(__name__)

# ── SECURITY CONFIGURATION ───────────────────────────────────────────────────

OTP_LENGTH = 6
OTP_EXPIRY_MINUTES = 5
RESEND_COOLDOWN_SECONDS = 60  # Must wait 60s before resend
MAX_OTP_PER_WINDOW = 3        # Max 3 OTP requests per 10 min
RATE_LIMIT_WINDOW_MINUTES = 10
MAX_VERIFY_ATTEMPTS = 5       # Max 5 verification attempts before lock


def _now() -> datetime:
    """Naive UTC datetime — compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _expires_at(minutes: int) -> datetime:
    """Naive UTC expiry — compatible with TIMESTAMP WITHOUT TIME ZONE columns."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(tzinfo=None)


# ── Security Audit Logging ───────────────────────────────────────────────────

def _log_security_event(event_type: str, email: str, phone: str, details: Dict[str, Any] = None):
    """Log security events for monitoring and debugging."""
    log_data = {
        "event": event_type,
        "email": mask_email(email),
        "phone": phone,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details or {}
    }

    if event_type in ["OTP_REQUESTED", "OTP_VERIFIED"]:
        logger.info(f"[SECURITY] {event_type}: {mask_email(email)} / {phone}")
    elif event_type in ["RATE_LIMITED", "BRUTE_FORCE_BLOCKED", "DUPLICATE_PHONE_BLOCKED"]:
        logger.warning(f"[SECURITY] {event_type}: {mask_email(email)} / {phone}")
    elif event_type in ["OTP_FAILED", "OTP_EXPIRED"]:
        logger.info(f"[SECURITY] {event_type}: {mask_email(email)} / {phone}, attempts: {details.get('attempts', 'N/A')}")
    else:
        logger.info(f"[SECURITY] {event_type}: {log_data}")


# ── OTP Generation & Hashing ─────────────────────────────────────────────────

def _generate_otp() -> str:
    """Generate a cryptographically secure 6-digit OTP."""
    # Use secrets for better randomness in production
    try:
        import secrets
        return ''.join(secrets.choice('0123456789') for _ in range(OTP_LENGTH))
    except ImportError:
        return ''.join(random.choices('0123456789', k=OTP_LENGTH))


def _hash_otp(otp: str) -> str:
    """Hash OTP using bcrypt (NEVER store raw OTP)."""
    return bcrypt.hashpw(otp.encode('utf-8'), bcrypt.gensalt(rounds=10)).decode('utf-8')


def _verify_otp_hash(otp: str, hashed: str) -> bool:
    """Verify OTP against bcrypt hash."""
    try:
        return bcrypt.checkpw(otp.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


# ── Rate Limiting ────────────────────────────────────────────────────────────

def _check_rate_limit(db: Session, email: str, phone: str) -> Tuple[bool, int, str]:
    """
    Check if user has exceeded OTP rate limit.

    Returns:
        (allowed: bool, seconds_until_reset: int, reason: str)
    """
    window_start = _now() - timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)

    # Count recent OTPs for this email OR phone
    recent_count = (
        db.query(OtpVerification)
        .filter(
            or_(
                OtpVerification.email == email.lower().strip(),
                OtpVerification.phone == phone
            ),
            OtpVerification.created_at >= window_start
        )
        .count()
    )

    if recent_count >= MAX_OTP_PER_WINDOW:
        # Find the oldest OTP in the window to calculate reset time
        oldest = (
            db.query(OtpVerification)
            .filter(
                or_(
                    OtpVerification.email == email.lower().strip(),
                    OtpVerification.phone == phone
                ),
                OtpVerification.created_at >= window_start
            )
            .order_by(OtpVerification.created_at.asc())
            .first()
        )

        if oldest:
            reset_time = oldest.created_at + timedelta(minutes=RATE_LIMIT_WINDOW_MINUTES)
            seconds_remaining = int((reset_time - _now()).total_seconds())
            return False, max(0, seconds_remaining), "rate_limit"

        return False, RATE_LIMIT_WINDOW_MINUTES * 60, "rate_limit"

    return True, 0, ""


def _check_resend_cooldown(db: Session, email: str, phone: str) -> Tuple[bool, int]:
    """
    Check if user can resend OTP (60-second cooldown).

    Returns:
        (can_resend: bool, seconds_remaining: int)
    """
    latest_otp = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.email == email.lower().strip(),
            OtpVerification.phone == phone
        )
        .order_by(OtpVerification.created_at.desc())
        .first()
    )

    if not latest_otp:
        return True, 0

    cooldown_end = latest_otp.created_at + timedelta(seconds=RESEND_COOLDOWN_SECONDS)
    now = _now()

    if now < cooldown_end:
        seconds_remaining = int((cooldown_end - now).total_seconds())
        return False, seconds_remaining

    return True, 0


# ── Phone Duplicate Protection ───────────────────────────────────────────────

def check_phone_exists_in_deal(db: Session, phone: str) -> Tuple[bool, Optional[Deal]]:
    """
    Check if phone number already has a verified non-deleted deal.
    Returns (exists: bool, deal: Deal or None)
    """
    deal = (
        db.query(Deal)
        .filter(Deal.customer_phone == phone, Deal.is_deleted == False)
        .first()
    )
    return deal is not None, deal


# ── Public API ──────────────────────────────────────────────────────────────

def send_otp_secure(db: Session, email: str, phone: str) -> Dict[str, Any]:
    """
    🔐 Send OTP to email with full security checks.

    Security Checks:
      1. Duplicate phone protection
      2. Rate limiting (3 per 10 min)
      3. Resend cooldown (60 seconds)
      4. Invalidates old OTP on resend

    Args:
        db: Database session
        email: Customer email address
        phone: Phone number to verify

    Returns:
        {
            "success": True,
            "message": str,
            "email_masked": str,
            "expires_in_seconds": int,
            "resend_after_seconds": int (if applicable)
        }

    Raises:
        ValueError: With specific error message for each security violation
    """
    email = email.lower().strip()

    # ── 1. DUPLICATE PHONE CHECK ────────────────────────────────────────────
    phone_exists, existing_deal = check_phone_exists_in_deal(db, phone)
    if phone_exists:
        _log_security_event(
            "DUPLICATE_PHONE_BLOCKED",
            email, phone,
            {"deal_id": existing_deal.id if existing_deal else None}
        )
        raise ValueError(
            "Phone number already used for another customer. "
            "Each phone can only be used for one deal."
        )

    # ── 2. RATE LIMITING ────────────────────────────────────────────────────
    allowed, seconds_remaining, reason = _check_rate_limit(db, email, phone)
    if not allowed:
        _log_security_event(
            "RATE_LIMITED",
            email, phone,
            {"seconds_remaining": seconds_remaining, "max_allowed": MAX_OTP_PER_WINDOW}
        )
        raise ValueError(
            f"Too many OTP requests. Please try again in {seconds_remaining // 60} minutes."
        )

    # ── 3. RESEND COOLDOWN ──────────────────────────────────────────────────
    can_resend, cooldown_seconds = _check_resend_cooldown(db, email, phone)
    if not can_resend:
        raise ValueError(
            f"Please wait {cooldown_seconds} seconds before requesting another OTP."
        )

    # ── 4. INVALIDATE OLD OTPs ──────────────────────────────────────────────
    # Mark any existing unverified OTPs as expired by setting verified=True
    # This prevents multiple active OTPs
    db.query(OtpVerification).filter(
        OtpVerification.email == email,
        OtpVerification.phone == phone,
        OtpVerification.verified == False
    ).update({"verified": True, "attempts": MAX_VERIFY_ATTEMPTS})  # Lock old OTPs

    # ── 5. GENERATE & HASH OTP ─────────────────────────────────────────────
    otp = _generate_otp()
    otp_hash = _hash_otp(otp)
    expires_at = _expires_at(OTP_EXPIRY_MINUTES)

    # ── 6. STORE IN DATABASE ───────────────────────────────────────────────
    otp_record = OtpVerification(
        email=email,
        phone=phone,
        otp_hash=otp_hash,
        expires_at=expires_at,
        verified=False,
        attempts=0,
        max_attempts=MAX_VERIFY_ATTEMPTS,
    )
    db.add(otp_record)
    db.commit()

    # ── 7. SEND EMAIL VIA RESEND ───────────────────────────────────────────
    email_sent = send_otp_email(email, otp, phone)

    if not email_sent:
        _log_security_event("OTP_DELIVERY_FAILED", email, phone)
        # Don't expose OTP if email fails
        raise ValueError("OTP delivery failed. Try again.")

    _log_security_event("OTP_REQUESTED", email, phone, {"expires_at": expires_at.isoformat() if expires_at else None})

    return {
        "success": True,
        "message": f"OTP sent to {mask_email(email)}",
        "email_masked": mask_email(email),
        "expires_in_seconds": OTP_EXPIRY_MINUTES * 60,
    }


def verify_otp_secure(db: Session, email: str, phone: str, otp: str) -> Dict[str, Any]:
    """
    🔐 Verify OTP with brute-force and replay attack protection.

    Security Checks:
      1. OTP exists and is not expired
      2. Not already verified (replay protection)
      3. Brute force: Max 5 attempts
      4. Hash comparison (NOT plaintext)
      5. Invalidate OTP after success

    Args:
        db: Database session
        email: Customer email address
        phone: Phone number
        otp: 6-digit OTP code

    Returns:
        {
            "success": True,
            "verified": True,
            "message": str,
            "phone": str
        }

    Raises:
        ValueError: With specific error message and remaining attempts
    """
    email = email.lower().strip()

    # ── 1. FIND LATEST ACTIVE OTP ───────────────────────────────────────────
    otp_record = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.email == email,
            OtpVerification.phone == phone,
            OtpVerification.verified == False
        )
        .order_by(OtpVerification.created_at.desc())
        .first()
    )

    if not otp_record:
        _log_security_event("OTP_VERIFY_NO_ACTIVE", email, phone)
        raise ValueError("No active OTP found. Please request a new OTP.")

    # ── 2. CHECK EXPIRY ─────────────────────────────────────────────────────
    now = _now()
    if otp_record.expires_at < now:
        _log_security_event("OTP_EXPIRED", email, phone, {"expired_at": otp_record.expires_at.isoformat()})
        raise ValueError("OTP has expired. Please request a new OTP.")

    # ── 3. CHECK IF ALREADY VERIFIED (REPLAY ATTACK) ────────────────────────
    if otp_record.verified:
        _log_security_event("OTP_REPLAY_ATTEMPT", email, phone)
        raise ValueError("This OTP has already been used. Please request a new OTP.")

    # ── 4. BRUTE FORCE CHECK ──────────────────────────────────────────────
    if otp_record.attempts >= otp_record.max_attempts:
        _log_security_event(
            "BRUTE_FORCE_BLOCKED",
            email, phone,
            {"attempts": otp_record.attempts, "max": otp_record.max_attempts}
        )
        raise ValueError(
            f"Maximum attempts ({otp_record.max_attempts}) exceeded. "
            "Please request a new OTP."
        )

    # ── 5. VERIFY OTP HASH ─────────────────────────────────────────────────
    if not _verify_otp_hash(otp, otp_record.otp_hash):
        # Increment attempt counter
        otp_record.attempts += 1
        db.commit()

        remaining = otp_record.max_attempts - otp_record.attempts

        _log_security_event(
            "OTP_FAILED",
            email, phone,
            {"attempts": otp_record.attempts, "remaining": remaining}
        )

        if remaining <= 0:
            raise ValueError(
                f"Maximum attempts exceeded. Please request a new OTP."
            )

        raise ValueError(f"Invalid OTP. {remaining} attempt(s) remaining.")

    # ── 6. SUCCESS — MARK AS LEGITIMATELY VERIFIED (REPLAY PROTECTION) ─────
    otp_record.verified = True
    otp_record.legitimately_verified = True  # ONLY set here — not during invalidation
    otp_record.verified_at = _now()
    db.commit()

    _log_security_event("OTP_VERIFIED", email, phone)

    return {
        "success": True,
        "verified": True,
        "message": "Phone number verified successfully",
        "phone": phone,
    }


def check_phone_security_status(db: Session, phone: str) -> Dict[str, Any]:
    """
    Check security status of a phone number.
    Used for UI feedback before OTP flow.
    """
    # Check if phone already has a deal
    has_deal, existing_deal = check_phone_exists_in_deal(db, phone)

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

    # Check for active OTP (pending verification)
    active_otp = (
        db.query(OtpVerification)
        .filter(
            OtpVerification.phone == phone,
            OtpVerification.verified == False,
            OtpVerification.expires_at > _now()
        )
        .order_by(OtpVerification.created_at.desc())
        .first()
    )

    return {
        "phone": phone,
        "can_proceed": not has_deal,
        "has_deal": has_deal,
        "is_verified": verified_otp is not None,
        "has_active_otp": active_otp is not None,
        "message": (
            "Phone number already used for another customer."
            if has_deal else None
        ),
    }


def is_phone_verified_for_deal(db: Session, phone: str) -> bool:
    """
    Check if a phone number has been legitimately verified (user entered correct OTP).
    Uses legitimately_verified to distinguish real verifications from invalidated OTPs.
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
