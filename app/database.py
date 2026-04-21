"""
Database engine, session factory, and dependency injection helper.

PostgreSQL only — no SQLite fallbacks, no conditional dialect logic.
Engine is configured for Supabase / Render PostgreSQL with connection
pooling tuned for a long-running FastAPI process.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,   # drop stale connections before use (handles Supabase idle timeouts)
    pool_size=5,          # persistent connections kept alive
    max_overflow=10,      # extra connections allowed under burst load
    pool_timeout=30,      # seconds to wait for a connection before raising
    pool_recycle=1800,    # recycle connections every 30 min (prevents TCP RST surprises)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a database session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
