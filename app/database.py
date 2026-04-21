"""
Database engine, session factory, and dependency injection helper.

PostgreSQL only — no SQLite fallbacks, no conditional dialect logic.
Engine is configured for Supabase / Render PostgreSQL with connection
pooling tuned for a long-running FastAPI process.

Slow query logging: any query exceeding SLOW_QUERY_MS is logged at WARNING
level so they appear in Render's log stream without flooding it.
"""

import logging
import time

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

_slow_log = logging.getLogger("aegibit.slow_query")

SLOW_QUERY_MS = 300  # log queries that take longer than this

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,   # drop stale connections before use (handles Supabase idle timeouts)
    pool_size=5,          # persistent connections kept alive
    max_overflow=10,      # extra connections allowed under burst load
    pool_timeout=30,      # seconds to wait for a connection before raising
    pool_recycle=1800,    # recycle connections every 30 min (prevents TCP RST surprises)
)


# ── Slow query instrumentation ────────────────────────────────────────────────

@event.listens_for(engine, "before_cursor_execute")
def _before_execute(conn, cursor, statement, parameters, context, executemany):
    conn.info["_query_start"] = time.monotonic()


@event.listens_for(engine, "after_cursor_execute")
def _after_execute(conn, cursor, statement, parameters, context, executemany):
    start = conn.info.pop("_query_start", None)
    if start is None:
        return
    elapsed_ms = (time.monotonic() - start) * 1000
    if elapsed_ms >= SLOW_QUERY_MS:
        # Truncate statement to 300 chars to avoid log flooding
        snippet = statement.replace("\n", " ").strip()[:300]
        _slow_log.warning("Slow query %.0fms: %s", elapsed_ms, snippet)


# ── Session factory ───────────────────────────────────────────────────────────

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a database session and ensures cleanup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
