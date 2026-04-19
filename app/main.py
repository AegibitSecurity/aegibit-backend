"""
AEGIBIT Flow — FastAPI Application Entry Point.

- Registers all routers
- Configures CORS
- Creates DB tables & seeds on startup
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.database import engine, Base, SessionLocal
from app.seed import seed_database
from app.database_migrations import run_all_migrations

# Import all routes
from app.routes import deals, tasks, notifications, upload, dashboard, websocket, models, auth, verification, email_verification, otp_secure


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: run migrations, create tables & seed. Shutdown: cleanup."""
    # Run schema migrations first (add missing columns)
    run_all_migrations(engine)

    # Create any new tables
    Base.metadata.create_all(bind=engine)

    # Seed with initial data
    db = SessionLocal()
    try:
        seed_database(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="AEGIBIT Flow",
    description="Real-time dealership deal decision engine",
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ──────────────────────────────────────────────────────────────────

app.include_router(deals.router)
app.include_router(tasks.router)
app.include_router(notifications.router)
app.include_router(upload.router)
app.include_router(dashboard.router)
app.include_router(websocket.router)
app.include_router(models.router)
app.include_router(auth.router)
app.include_router(verification.router)
app.include_router(email_verification.router)
app.include_router(otp_secure.router, prefix="/api/v1")


# ── Org listing (needed by frontend) ─────────────────────────────────────────

from app.database import get_db
from fastapi import Depends
from sqlalchemy.orm import Session
from app.models import Organization
from app.auth import get_current_user, AuthContext


@app.get("/organizations")
def list_organizations(
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(get_current_user),
):
    """Return all organizations for the frontend org switcher (authenticated)."""
    orgs = db.query(Organization).order_by(Organization.name).all()
    return [{"id": o.id, "name": o.name} for o in orgs]
