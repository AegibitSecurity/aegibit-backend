"""
Email Verification Routes — Phone verification via Email OTP

Endpoints:
  POST /send-email-otp    → Send OTP to email
  POST /verify-email-otp  → Verify OTP
  GET  /check-phone       → Check phone status (exists, verified)
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_current_user
from app.services import email_otp_service as otp_svc
from app.schemas import (
    SendEmailOtpRequest,
    SendEmailOtpResponse,
    VerifyEmailOtpRequest,
    VerifyEmailOtpResponse,
    CheckPhoneResponse,
)

router = APIRouter(tags=["email-verification"])


@router.post("/send-email-otp", response_model=SendEmailOtpResponse)
def send_email_otp(
    body: SendEmailOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Send OTP to email for phone verification.

    - Checks if phone already exists in deals (unique constraint)
    - Rate limited: max 3 OTPs per 10 minutes
    - OTP expires in 5 minutes
    - OTP is hashed (never stored plain text)
    """
    try:
        result = otp_svc.send_email_otp(db, body.email, body.phone)
        return SendEmailOtpResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send OTP: {str(e)}")


@router.post("/verify-email-otp", response_model=VerifyEmailOtpResponse)
def verify_email_otp(
    body: VerifyEmailOtpRequest,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Verify OTP sent to email.

    - Max 3 attempts per OTP
    - OTP expires after 5 minutes
    - Returns remaining attempts on failure
    """
    try:
        result = otp_svc.verify_email_otp(db, body.email, body.phone, body.otp)
        return VerifyEmailOtpResponse(**result)
    except ValueError as e:
        # Check if it mentions remaining attempts
        error_msg = str(e)
        if "remaining" in error_msg.lower():
            # Extract remaining attempts from message
            import re
            match = re.search(r'(\d+) attempt', error_msg)
            remaining = int(match.group(1)) if match else None
            raise HTTPException(
                status_code=400,
                detail={
                    "message": error_msg,
                    "remaining_attempts": remaining
                }
            )
        raise HTTPException(status_code=400, detail=error_msg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Verification failed: {str(e)}")


@router.get("/check-phone", response_model=CheckPhoneResponse)
def check_phone(
    phone: str,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Check if phone number exists and is verified.
    Returns status for UI feedback before OTP flow.
    """
    try:
        result = otp_svc.check_phone_status(db, phone)
        return CheckPhoneResponse(**result)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to check phone: {str(e)}")
