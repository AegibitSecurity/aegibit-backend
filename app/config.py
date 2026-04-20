"""
Application configuration — reads from environment variables with sensible defaults.
"""

import json
import logging
import logging.config
import os

logger = logging.getLogger(__name__)


def _configure_logging(log_level: str) -> None:
    """Configure JSON-structured logging for production, plain text for dev."""
    numeric = getattr(logging, log_level.upper(), logging.INFO)
    environment = os.getenv("ENVIRONMENT", "development")

    if environment == "production":
        class _JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                import traceback
                entry: dict = {
                    "ts":      self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
                    "level":   record.levelname,
                    "logger":  record.name,
                    "msg":     record.getMessage(),
                }
                if record.exc_info:
                    entry["exc"] = traceback.format_exception(*record.exc_info)[-1].strip()
                return json.dumps(entry)

        handler = logging.StreamHandler()
        handler.setFormatter(_JsonFormatter())
    else:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(numeric)
    root.handlers.clear()
    root.addHandler(handler)
    # Quiet noisy libraries
    for noisy in ("uvicorn.access", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


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

    # ── Core ──────────────────────────────────────────────────────────────────
    APP_VERSION: str       = "1.0.0"
    ENVIRONMENT: str       = os.getenv("ENVIRONMENT", "development")
    DATABASE_URL: str      = os.getenv("DATABASE_URL", "sqlite:///./aegibit_flow.db")
    CORS_ORIGINS: list[str] = _cors_origins()
    HOST: str              = os.getenv("HOST", "0.0.0.0")
    PORT: int              = int(os.getenv("PORT", "8000"))

    # ── Auth / JWT ────────────────────────────────────────────────────────────
    JWT_SECRET: str        = _jwt_secret()
    JWT_ALGORITHM: str     = "HS256"
    JWT_EXPIRY_HOURS: int  = int(os.getenv("JWT_EXPIRY_HOURS", "24"))

    # ── Sentry ────────────────────────────────────────────────────────────────
    SENTRY_DSN: str        = os.getenv("SENTRY_DSN", "")

    # ── Cookie auth ───────────────────────────────────────────────────────────
    COOKIE_SECURE: bool    = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    COOKIE_SAMESITE: str   = os.getenv("COOKIE_SAMESITE", "lax")

    # ── Request limits ────────────────────────────────────────────────────────
    # Max body size in bytes (default 10 MB). Raise for file-upload routes only.
    MAX_REQUEST_BODY_BYTES: int = int(os.getenv("MAX_REQUEST_BODY_MB", "10")) * 1024 * 1024

    # ── Logging ───────────────────────────────────────────────────────────────
    LOG_LEVEL: str         = os.getenv("LOG_LEVEL", "INFO")


settings = Settings()
_configure_logging(settings.LOG_LEVEL)
