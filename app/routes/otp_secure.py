"""
🔐 Secure OTP Routes — Production-grade phone verification

Endpoints:
  POST /send-otp    → Send OTP with full security checks
  POST /verify-otp  → Verify OTP with brute-force protection
  GET  /check-phone → Check phone security status

Security:
  - Rate limiting
  - Duplicate phone protection
  - Brute force protection
  - Comprehensive logging
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr, Field
from typing import Optional

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.services import otp_service_secure as otp_svc

router = APIRouter(tags=["otp-secure"])


# ── Request/Response Schemas ───────────────────────────────────────────────

class SendOtpRequest(BaseModel):
    email: EmailStr = Field(..., description="Customer email address")
    phone: str = Field(..., min_length=10, max_length=15, description="Phone number to verify")


class SendOtpResponse(BaseModel):
    success: bool
    message: str
    email_masked: str
    expires_in_seconds: int


class VerifyOtpRequest(BaseModel):
    email: EmailStr = Field(..., description="Customer email address")
    phone: str = Field(..., min_length=10, max_length=15, description="Phone number")
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit OTP code")


class VerifyOtpResponse(BaseModel):
    success: bool
    verified: bool
    message: str
    phone: str


class CheckPhoneResponse(BaseModel):
    phone: str
    can_proceed: bool
    has_deal: bool
    is_verified: bool
    has_active_otp: bool
    message: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str


# ── Routes ─────────────────────────────────────────────────────────────────

@router.post(
    "/send-otp",
    response_model=SendOtpResponse,
    responses={
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        400: {"model": ErrorResponse, "description": "Invalid input or duplicate phone"},
        500: {"model": ErrorResponse, "description": "Email delivery failed"},
    }
)
def send_otp(
    body: SendOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    🔐 Send OTP to email with full security protection.

    Security Checks:
      - Duplicate phone protection (phone already in deal → BLOCK)
      - Rate limiting (max 3 OTPs per 10 min)
      - Resend cooldown (60 seconds between requests)
      - Old OTPs invalidated on resend

    Email sent via Resend API with professional HTML template.
    """
    try:
        result = otp_svc.send_otp_secure(db, body.email, body.phone)
        return SendOtpResponse(**result)

    except ValueError as e:
        error_msg = str(e)

        # Determine appropriate status code
        if "already used" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=error_msg
            )
        elif "Too many" in error_msg or "wait" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error_msg
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"OTP delivery failed: {str(e)}"
        )


@router.post(
    "/verify-otp",
    response_model=VerifyOtpResponse,
    responses={
        400: {"model": ErrorResponse, "description": "Invalid OTP or expired"},
        423: {"model": ErrorResponse, "description": "Locked due to too many attempts"},
    }
)
def verify_otp(
    body: VerifyOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    🔐 Verify OTP with brute-force and replay attack protection.

    Security Checks:
      - OTP exists and not expired
      - Not already used (replay protection)
      - Max 5 verification attempts
      - Hash comparison (NOT plaintext)
      - OTP invalidated immediately after success

    On success, phone is marked as verified and can be used for deal creation.
    """
    try:
        result = otp_svc.verify_otp_secure(db, body.email, body.phone, body.otp)
        return VerifyOtpResponse(**result)

    except ValueError as e:
        error_msg = str(e)

        # Check if locked out
        if "Maximum attempts exceeded" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail=error_msg
            )

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg
        )

    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Verification failed: {str(e)}"
        )


@router.get(
    "/check-phone",
    response_model=CheckPhoneResponse
)
def check_phone(
    phone: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    🔍 Check phone security status before initiating OTP flow.

    Returns:
      - can_proceed: False if phone already used
      - has_deal: True if phone has existing deal
      - is_verified: True if phone already verified
      - has_active_otp: True if pending OTP exists
      - message: Error message if blocked
    """
    try:
        result = otp_svc.check_phone_security_status(db, phone)
        return CheckPhoneResponse(**result)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to check phone status: {str(e)}"
        )
