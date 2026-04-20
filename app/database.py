"""
Database engine, session factory, and dependency injection helper.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # SQLite: single writer, no connection pool needed
    engine = create_engine(
        settings.DATABASE_URL,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )
else:
    # PostgreSQL / MySQL: connection pool tuned for production
    engine = create_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,      # test connections before use (handles idle timeouts)
        pool_size=10,            # keep up to 10 persistent connections
        max_overflow=20,         # allow up to 20 extra connections under burst
        pool_timeout=30,         # raise after 30s if no connection available
        pool_recycle=1800,       # recycle connections every 30 min (prevents stale TCP)
    )

# Enable WAL mode and foreign keys for SQLite
if _is_sqlite:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a database session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
