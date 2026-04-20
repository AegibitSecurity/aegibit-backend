"""
AEGIBIT Flow — FastAPI Application Entry Point.

Architecture:
  - All API routes under /api/v1 prefix
  - HTTP-only cookie authentication (no tokens in JS)
  - Standard response envelope: { success, data, error, request_id }
  - X-Request-ID on every response
  - Sentry error monitoring (set SENTRY_DSN env var to enable)
  - Rate limiting on login endpoint via slowapi
"""

from contextlib import asynccontextmanager
import logging
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.starlette import StarletteIntegration

from fastapi import FastAPI, Depends, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import engine, Base, SessionLocal, get_db
from app.seed import seed_database
from app.database_migrations import run_all_migrations
from app.middleware import StandardResponseMiddleware
from app.limiter import limiter

logger = logging.getLogger(__name__)

# ── Import all route modules ──────────────────────────────────────────────────
from app.routes import (
    deals, tasks, notifications, upload, dashboard,
    websocket, models, auth, verification, email_verification, otp_secure,
)


# ── Sentry — initialize before app creation so it catches startup errors ──────
if settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        integrations=[
            StarletteIntegration(transaction_style="endpoint"),
            FastApiIntegration(transaction_style="endpoint"),
        ],
        traces_sample_rate=1.0,
        # Never send passwords, tokens, or cookie values to Sentry
        send_default_pii=False,
    )


# ── Request body size limit middleware ───────────────────────────────────────
# Rejects requests whose Content-Length exceeds MAX_REQUEST_BODY_BYTES.
# File upload route (/upload-excel) is exempt — it sets its own 60s timeout.

class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if (
            content_length
            and "/upload-excel" not in request.url.path
            and int(content_length) > settings.MAX_REQUEST_BODY_BYTES
        ):
            return Response(
                content='{"detail": "Request body too large"}',
                status_code=413,
                media_type="application/json",
            )
        return await call_next(request)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: run migrations, create tables, seed. Shutdown: no-op."""
    logger.info("Starting AEGIBIT Flow %s [%s]", settings.APP_VERSION, settings.ENVIRONMENT)
    run_all_migrations(engine)
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    logger.info("Startup complete — database ready")
    yield
    logger.info("Shutdown complete")


# ── Application ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="AEGIBIT Flow",
    description="Real-time dealership deal decision engine",
    version="1.0.0",
    lifespan=lifespan,
    # OpenAPI docs only show /api/v1 routes
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
)

# ── Rate limiter ──────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ──────────────────────────────────────────────────────────────────────
# allow_credentials=True is required for cross-origin cookies.
# A wildcard origin ("*") is incompatible with credentials — never use it.

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_origin_regex=r"https://aegibit-frontend-.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Standard response envelope middleware ─────────────────────────────────────
# Must be added AFTER CORSMiddleware so CORS headers are written first.
# Wraps all /api/v1 JSON responses in { success, data, error, request_id }.

app.add_middleware(StandardResponseMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)

# ── Health checks (no auth, no envelope) ─────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return {"status": "ok", "version": settings.APP_VERSION}


@app.api_route("/health", methods=["GET", "HEAD"])
def health(db=Depends(get_db)):
    """
    Deep health check — verifies DB connectivity.
    Load balancers and uptime monitors should call this.
    Returns 200 when healthy, 503 when the database is unreachable.
    """
    from sqlalchemy import text
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        logger.error("Health check: database unreachable — %s", exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "database": "unreachable",
                "version": settings.APP_VERSION,
            },
        )
    return {
        "status": "healthy",
        "database": db_status,
        "version": settings.APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }


# ── Versioned routers (/api/v1/...) ──────────────────────────────────────────
#
# Route conflict resolution:
#   verification.py   → /api/v1/phone/...    (phone SMS OTP, legacy)
#   email_verification → /api/v1/email-otp/... (email OTP v1, legacy)
#   otp_secure        → /api/v1/...           (email OTP v2, active — no sub-prefix)
#
# This prevents path collisions between the three OTP systems
# (all had /send-otp, /verify-otp, /check-phone routes at root level).

app.include_router(auth.router,               prefix="/api/v1")
app.include_router(deals.router,              prefix="/api/v1")
app.include_router(tasks.router,              prefix="/api/v1")
app.include_router(notifications.router,      prefix="/api/v1")
app.include_router(upload.router,             prefix="/api/v1")
app.include_router(dashboard.router,          prefix="/api/v1")
app.include_router(websocket.router,          prefix="/api/v1")
app.include_router(models.router,             prefix="/api/v1")
app.include_router(otp_secure.router,         prefix="/api/v1")           # /api/v1/send-otp, etc.
app.include_router(verification.router,       prefix="/api/v1/phone")     # /api/v1/phone/check-customer, etc.
app.include_router(email_verification.router, prefix="/api/v1/email-otp") # /api/v1/email-otp/send-email-otp, etc.


# ── Organizations (inline — too small to merit its own router file) ───────────

from sqlalchemy.orm import Session
from app.models import Organization
from app.auth import get_current_user, AuthContext


@app.get("/api/v1/organizations")
def list_organizations(
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_current_user),
):
    """Return all organizations for the frontend org switcher (authenticated)."""
    orgs = db.query(Organization).order_by(Organization.name).all()
    return [{"id": o.id, "name": o.name} for o in orgs]
