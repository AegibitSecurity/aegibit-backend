"""
Safe Database Migrations — Add missing columns without deleting data.

This module provides idempotent schema patches for SQLite.
Runs automatically on FastAPI startup.
"""

from sqlalchemy import text, inspect
from sqlalchemy.engine import Engine
import logging

logger = logging.getLogger(__name__)


def _column_exists(engine: Engine, table_name: str, column_name: str) -> bool:
    """Check if a column exists in a table using PRAGMA."""
    with engine.connect() as conn:
        result = conn.execute(text(f"PRAGMA table_info({table_name})"))
        columns = [row[1] for row in result]  # row[1] is column name
        return column_name in columns


def _add_column(engine: Engine, table_name: str, column_name: str, column_def: str):
    """Add a column to a table. Returns True if added, False if already exists."""
    if _column_exists(engine, table_name, column_name):
        logger.info(f"Column '{column_name}' already exists in '{table_name}'")
        return False

    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"))
        conn.commit()
        logger.info(f"Added column '{column_name}' to '{table_name}'")
        return True


def migrate_deals_table(engine: Engine):
    """
    Add missing columns to deals table (idempotent).

    Columns to add if missing:
    - customer_id (VARCHAR(36)) - FK to customers
    - salesperson_id (VARCHAR(36)) - FK to users
    - customer_phone (VARCHAR(15)) - UNIQUE customer's phone
    - phone_verified (BOOLEAN DEFAULT 0) - Email OTP verification status
    - customer_email (VARCHAR(255)) - For OTP delivery
    - is_deleted (BOOLEAN DEFAULT 0) - Soft delete flag
    - deleted_at (DATETIME) - When deal was deleted
    - deleted_by (VARCHAR(36)) - Who deleted the deal
    """
    table_name = "deals"

    # Check table exists first
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": table_name}
        )
        if not result.fetchone():
            logger.info(f"Table '{table_name}' does not exist yet, skipping migration")
            return

    migrations = [
        ("customer_id", "VARCHAR(36)"),
        ("salesperson_id", "VARCHAR(36)"),
        ("customer_phone", "VARCHAR(15)"),
        ("phone_verified", "BOOLEAN DEFAULT 0"),
        ("customer_email", "VARCHAR(255)"),
        ("is_deleted", "BOOLEAN DEFAULT 0"),
        ("deleted_at", "DATETIME"),
        ("deleted_by", "VARCHAR(36)"),
    ]

    added_count = 0
    for column_name, column_def in migrations:
        if _add_column(engine, table_name, column_name, column_def):
            added_count += 1

    if added_count == 0:
        logger.info(f"All columns already exist in '{table_name}'")
    else:
        logger.info(f"Migration complete: added {added_count} column(s) to '{table_name}'")


def migrate_otp_verifications_table(engine: Engine):
    """
    Create otp_verifications table if it doesn't exist, then patch missing columns.
    """
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='otp_verifications'")
        )
        if not result.fetchone():
            conn.execute(text("""
                CREATE TABLE otp_verifications (
                    id VARCHAR(36) PRIMARY KEY,
                    email VARCHAR(255) NOT NULL,
                    phone VARCHAR(15) NOT NULL,
                    otp_hash VARCHAR(255) NOT NULL,
                    expires_at DATETIME NOT NULL,
                    verified BOOLEAN DEFAULT 0,
                    legitimately_verified BOOLEAN DEFAULT 0,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 3,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    verified_at DATETIME
                )
            """))
            conn.execute(text("CREATE INDEX ix_otp_email ON otp_verifications(email)"))
            conn.execute(text("CREATE INDEX ix_otp_phone ON otp_verifications(phone)"))
            conn.execute(text("CREATE INDEX ix_otp_expires ON otp_verifications(expires_at)"))
            conn.commit()
            logger.info("Created table 'otp_verifications' with indexes")
            return

    # Table already exists — add legitimately_verified if missing
    _add_column(engine, "otp_verifications", "legitimately_verified", "BOOLEAN DEFAULT 0")
    logger.info("otp_verifications table migration complete")


def run_all_migrations(engine: Engine):
    """
    Run all database migrations.
    Called on FastAPI startup.
    """
    logger.info("Starting database migrations...")

    try:
        migrate_deals_table(engine)
        migrate_otp_verifications_table(engine)
        logger.info("Database migrations completed successfully")
    except Exception as e:
        logger.error(f"Database migration failed: {e}")
        # Don't re-raise — we don't want to prevent app startup
        # Log the error and continue
