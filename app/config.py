"""
Application configuration — reads from environment variables with sensible defaults.
"""

import logging
import os
import secrets

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
        # Generate a temporary secret — sessions will not survive restarts.
        # Set JWT_SECRET env var in production.
        generated = secrets.token_hex(32)
        logger.warning(
            "JWT_SECRET env var is not set. Using a temporary secret — "
            "all sessions will be invalidated on every server restart. "
            "Set JWT_SECRET in your environment or .env file."
        )
        return generated
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


settings = Settings()
