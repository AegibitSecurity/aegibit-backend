"""
Email Service — Send OTP and other transactional emails

Supports:
  - SMTP (Gmail, etc.)
  - Resend API (recommended for production)
  - Console output (development fallback)

Configuration via environment variables:
  EMAIL_PROVIDER=smtp|resend|console
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USER=your@gmail.com
  SMTP_PASS=your_app_password
  RESEND_API_KEY=re_xxxxxxxx
  FROM_EMAIL=noreply@yourdomain.com
  FROM_NAME=Your App Name
"""

import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ── Configuration ───────────────────────────────────────────────────────────

# ── Configuration ───────────────────────────────────────────────────────────
# SECURITY: Resend API Key for production OTP delivery
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "re_C4cJUwqD_MZqK5Xt8KWevwCs5V1zkvNuJ")
EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "resend")  # resend | smtp | console

# SMTP fallback settings
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

# Sender identity
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@aegibit.com")
FROM_NAME = os.getenv("FROM_NAME", "AEGIBIT Flow")


# ── Email Templates ─────────────────────────────────────────────────────────

def get_otp_email_template(otp: str, phone: str) -> tuple[str, str, str]:
    """
    Returns (subject, html_body, text_body) for OTP email.
    Professional HTML template for Resend API.
    """
    subject = "Your OTP Code - AEGIBIT Flow"

    # Professional HTML template matching user requirements
    html_body = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Your OTP Code</title>
    <style>
        body {{ font-family: Arial, sans-serif; background: #f4f4f4; margin: 0; padding: 20px; }}
        .container {{ max-width: 400px; margin: 0 auto; background: white; padding: 40px; border-radius: 8px; text-align: center; }}
        h2 {{ color: #333; margin-bottom: 20px; }}
        .otp-code {{ font-size: 48px; font-weight: bold; color: #6366f1; letter-spacing: 8px; margin: 30px 0; font-family: monospace; }}
        p {{ color: #666; line-height: 1.6; }}
        .expiry {{ color: #e74c3c; font-weight: bold; margin-top: 20px; }}
        .footer {{ margin-top: 30px; font-size: 12px; color: #999; }}
    </style>
</head>
<body>
    <div class="container">
        <h2>Your OTP Code</h2>
        <p>Your verification code for phone number {phone} is:</p>
        <div class="otp-code">{otp}</div>
        <p class="expiry">This code expires in 5 minutes.</p>
        <div class="footer">
            <p>AEGIBIT Flow — Secure Dealership Platform</p>
            <p>If you didn't request this code, please ignore this email.</p>
        </div>
    </div>
</body>
</html>
    """

    text_body = f"""
Phone Verification — AEGIBIT Flow

Your OTP code is: {otp}
Phone number: {phone}

Valid for 5 minutes only.

If you didn't request this verification, please ignore this email.
    """.strip()

    return subject, html_body, text_body


# ── Email Senders ────────────────────────────────────────────────────────────

def _send_via_smtp(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send email via SMTP (Gmail, etc.)"""
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
        msg["To"] = to_email

        msg.attach(MIMEText(text_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(FROM_EMAIL, to_email, msg.as_string())

        logger.info(f"Email sent via SMTP to {to_email}")
        return True

    except Exception as e:
        logger.error(f"SMTP send failed: {e}")
        return False


def _send_via_resend(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Send email via Resend API"""
    try:
        import requests

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "from": f"{FROM_NAME} <{FROM_EMAIL}>",
                "to": [to_email],
                "subject": subject,
                "html": html_body,
                "text": text_body,
            },
            timeout=30,
        )

        if response.status_code in (200, 202):
            logger.info(f"Email sent via Resend to {to_email}")
            return True
        else:
            logger.error(f"Resend API error: {response.status_code} - {response.text}")
            return False

    except Exception as e:
        logger.error(f"Resend send failed: {e}")
        return False


def _send_via_console(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """Development fallback — prints to console"""
    print("\n" + "=" * 60)
    print("📧 EMAIL (Console Mode)")
    print("=" * 60)
    print(f"To: {to_email}")
    print(f"Subject: {subject}")
    print("-" * 60)
    print(text_body)
    print("=" * 60 + "\n")
    return True


# ── Public API ──────────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, html_body: str, text_body: str) -> bool:
    """
    Send an email using the configured provider.
    Returns True if sent successfully, False otherwise.
    """
    if EMAIL_PROVIDER == "smtp":
        return _send_via_smtp(to_email, subject, html_body, text_body)
    elif EMAIL_PROVIDER == "resend":
        return _send_via_resend(to_email, subject, html_body, text_body)
    else:
        return _send_via_console(to_email, subject, html_body, text_body)


def send_otp_email(email: str, otp: str, phone: str) -> bool:
    """
    Send OTP verification email.
    Returns True if sent successfully.
    """
    subject, html_body, text_body = get_otp_email_template(otp, phone)
    return send_email(email, subject, html_body, text_body)


def mask_email(email: str) -> str:
    """
    Mask email for display: user@example.com → u***@example.com
    """
    if "@" not in email:
        return "***"

    local, domain = email.split("@", 1)
    if len(local) <= 2:
        masked_local = local[0] + "***"
    else:
        masked_local = local[0] + "***" + local[-1] if len(local) > 3 else local[0] + "***"

    return f"{masked_local}@{domain}"
