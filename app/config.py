"""
Application configuration — reads from environment variables with sensible defaults.
"""

import logging
import os

logger = logging.getLogger(__name__)


def _cors_origins() -> list[str]:
    """
    Build CORS origin list.
    In production set CORS_ORIGINS env var (comma-separated, no spaces).
    Example: CORS_ORIGINS=https://app.aegibit.com,https://www.aegibit.com
    The wildcard '*' is never included automatically.
    """
    raw = os.getenv("CORS_ORIGINS", "")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # Dev-only defaults — localhost only, no wildcard
    return [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]


def _jwt_secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if not secret:
        raise RuntimeError(
            "JWT_SECRET environment variable is not set. "
            "Generate one with: python -c \"import secrets; print(secrets.token_hex(64))\" "
            "and set it in your Render environment variables."
        )
    return secret


class Settings:
    """Central configuration for the AEGIBIT Flow backend."""

    DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./aegibit_flow.db")
    CORS_ORIGINS: list[str] = _cors_origins()
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))
    JWT_SECRET: str = _jwt_secret()
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_HOURS: int = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

    # ── Sentry ────────────────────────────────────────────────────────────────
    # Set SENTRY_DSN in production. Empty string disables Sentry.
    SENTRY_DSN: str = os.getenv("SENTRY_DSN", "")

    # ── Cookie auth ───────────────────────────────────────────────────────────
    # COOKIE_SECURE=false   in development (http://localhost)
    # COOKIE_SECURE=true    in production  (https://)
    COOKIE_SECURE: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    # COOKIE_SAMESITE=lax   for same-site deployments
    # COOKIE_SAMESITE=none  for cross-origin (Vercel frontend + Render backend)
    # When samesite=none, COOKIE_SECURE MUST be true (browsers enforce this)
    COOKIE_SAMESITE: str = os.getenv("COOKIE_SAMESITE", "lax")


settings = Settings()
