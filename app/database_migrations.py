"""
Safe Database Migrations — Add missing columns without deleting data.

PostgreSQL-native. All schema introspection uses SQLAlchemy Inspector —
zero raw sqlite_master / PRAGMA queries.

Every function is idempotent: safe to call on every startup.
"""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# ── Schema introspection helpers ──────────────────────────────────────────────

def _table_exists(engine: Engine, table_name: str) -> bool:
    """Return True if table exists in the public schema."""
    return inspect(engine).has_table(table_name)


def _column_exists(engine: Engine, table_name: str, column_name: str) -> bool:
    """Return True if column exists in the given table."""
    cols = [c["name"] for c in inspect(engine).get_columns(table_name)]
    return column_name in cols


def _add_column(
    engine: Engine, table_name: str, column_name: str, column_def: str
) -> bool:
    """
    Add a column if it does not already exist.
    Returns True if column was added, False if it already existed.
    Uses engine.begin() so the ALTER TABLE is auto-committed on success.
    """
    if _column_exists(engine, table_name, column_name):
        logger.debug("Column '%s' already exists in '%s' — skipping", column_name, table_name)
        return False

    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
        )
    logger.info("Added column '%s' to '%s'", column_name, table_name)
    return True


# ── Per-table migrations ──────────────────────────────────────────────────────

def migrate_deals_table(engine: Engine) -> None:
    """Add missing columns to the deals table (idempotent)."""
    if not _table_exists(engine, "deals"):
        logger.info("Table 'deals' does not exist yet — skipping migration")
        return

    migrations = [
        ("customer_id",     "VARCHAR(36)"),
        ("salesperson_id",  "VARCHAR(36)"),
        ("customer_phone",  "VARCHAR(15)"),
        ("phone_verified",  "BOOLEAN DEFAULT FALSE"),
        ("customer_email",  "VARCHAR(255)"),
        ("is_deleted",      "BOOLEAN DEFAULT FALSE"),
        ("deleted_at",      "TIMESTAMP"),
        ("deleted_by",      "VARCHAR(36)"),
    ]

    added = sum(
        1 for col, defn in migrations
        if _add_column(engine, "deals", col, defn)
    )

    if added:
        logger.info("deals migration: added %d column(s)", added)
    else:
        logger.info("deals migration: all columns already present")


def migrate_otp_verifications_table(engine: Engine) -> None:
    """Create otp_verifications table if absent, then patch missing columns."""
    if not _table_exists(engine, "otp_verifications"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE otp_verifications (
                    id                    VARCHAR(36)  PRIMARY KEY,
                    email                 VARCHAR(255) NOT NULL,
                    phone                 VARCHAR(15)  NOT NULL,
                    otp_hash              VARCHAR(255) NOT NULL,
                    expires_at            TIMESTAMP    NOT NULL,
                    verified              BOOLEAN      DEFAULT FALSE,
                    legitimately_verified BOOLEAN      DEFAULT FALSE,
                    attempts              INTEGER      DEFAULT 0,
                    max_attempts          INTEGER      DEFAULT 3,
                    created_at            TIMESTAMP    DEFAULT NOW(),
                    verified_at           TIMESTAMP
                )
            """))
            conn.execute(text(
                "CREATE INDEX ix_otp_email   ON otp_verifications(email)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_otp_phone   ON otp_verifications(phone)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_otp_expires ON otp_verifications(expires_at)"
            ))
        logger.info("Created table 'otp_verifications' with indexes")
        return

    # Table already exists — backfill any missing columns
    _add_column(
        engine, "otp_verifications",
        "legitimately_verified", "BOOLEAN DEFAULT FALSE"
    )
    logger.info("otp_verifications migration complete")


def migrate_audit_logs_table(engine: Engine) -> None:
    """Create audit_logs table if absent, then patch missing columns."""
    if not _table_exists(engine, "audit_logs"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE audit_logs (
                    id                 VARCHAR(36)  PRIMARY KEY,
                    organization_id    VARCHAR(36)  NOT NULL,
                    action_type        VARCHAR(50)  NOT NULL,
                    entity_type        VARCHAR(50)  NOT NULL,
                    entity_id          VARCHAR(36)  NOT NULL,
                    performed_by       VARCHAR(36),
                    performed_by_email VARCHAR(255),
                    ip_address         VARCHAR(45),
                    previous_data      TEXT,
                    new_data           TEXT,
                    note               TEXT,
                    created_at         TIMESTAMP    DEFAULT NOW(),
                    row_hash           VARCHAR(64),
                    prev_hash          VARCHAR(64)
                )
            """))
            conn.execute(text(
                "CREATE INDEX ix_audit_org_created  ON audit_logs(organization_id, created_at)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_audit_entity       ON audit_logs(entity_type, entity_id)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_audit_performed_by ON audit_logs(performed_by)"
            ))
            conn.execute(text(
                "CREATE INDEX ix_audit_action       ON audit_logs(action_type)"
            ))
        logger.info("Created table 'audit_logs' with hash-chain columns and indexes")
        return

    # Table already exists — backfill hash-chain columns
    _add_column(engine, "audit_logs", "row_hash",  "VARCHAR(64)")
    _add_column(engine, "audit_logs", "prev_hash", "VARCHAR(64)")
    logger.info("audit_logs migration complete")


def migrate_branches_table(engine: Engine) -> None:
    """Create branches table if absent, then patch missing columns (idempotent)."""
    if not _table_exists(engine, "branches"):
        with engine.begin() as conn:
            conn.execute(text("""
                CREATE TABLE branches (
                    id              VARCHAR(36)  PRIMARY KEY,
                    organization_id VARCHAR(36)  NOT NULL REFERENCES organizations(id),
                    name            VARCHAR(255) NOT NULL,
                    is_head_office  BOOLEAN      NOT NULL DEFAULT FALSE,
                    created_at      TIMESTAMP    DEFAULT NOW()
                )
            """))
            conn.execute(text(
                "CREATE INDEX ix_branch_org ON branches(organization_id)"
            ))
        logger.info("Created table 'branches' with index")
    else:
        _add_column(engine, "branches", "is_head_office", "BOOLEAN NOT NULL DEFAULT FALSE")
        logger.info("branches migration complete")


def migrate_user_branch(engine: Engine) -> None:
    """Add branch_id FK to users table (idempotent)."""
    if not _table_exists(engine, "users"):
        return
    _add_column(engine, "users", "branch_id", "VARCHAR(36) REFERENCES branches(id)")
    logger.info("users branch_id migration complete")


def migrate_deal_branch(engine: Engine) -> None:
    """Add branch_id FK to deals table (idempotent)."""
    if not _table_exists(engine, "deals"):
        return
    _add_column(engine, "deals", "branch_id", "VARCHAR(36) REFERENCES branches(id)")
    logger.info("deals branch_id migration complete")


def migrate_phone_constraint(engine: Engine) -> None:
    """
    Replace the full UNIQUE constraint on deals.customer_phone with a partial
    unique index that excludes soft-deleted rows.

    Problem: uq_deal_customer_phone applies to ALL rows, so soft-deleted deals
    permanently block that phone from ever being reused — even after restore.

    Fix: drop the constraint, create a partial index instead so that only
    non-deleted deals block the phone.

    Idempotent — safe to run on every startup.
    """
    if not _table_exists(engine, "deals"):
        return

    with engine.begin() as conn:
        # Drop the full unique constraint if it still exists
        conn.execute(text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_deal_customer_phone'
                ) THEN
                    ALTER TABLE deals DROP CONSTRAINT uq_deal_customer_phone;
                END IF;
            END$$
        """))
        # Create the partial unique index (idempotent via IF NOT EXISTS)
        conn.execute(text("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_deal_customer_phone_active
            ON deals(customer_phone)
            WHERE is_deleted = FALSE AND customer_phone IS NOT NULL
        """))
    logger.info("Phone constraint migrated to partial unique index")


def migrate_indexes(engine: Engine) -> None:
    """
    Create missing performance indexes using CREATE INDEX IF NOT EXISTS
    (PostgreSQL 9.5+, idempotent).

    Indexes added:
      ix_deal_delivery_org   — partial index on (org, delivery_date) WHERE delivery_date IS NOT NULL
                               Powers the upcoming-deliveries query (was a full table scan).
      ix_deal_org_deleted    — (org, is_deleted, status) composite
                               Powers the dashboard aggregation query.
      ix_task_pending        — partial index on (deal_id) WHERE status = 'PENDING'
                               Powers pending task counts without scanning completed tasks.
    """
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_branch_org_name
            ON branches(organization_id, name)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_user_branch
            ON users(branch_id)
            WHERE branch_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_deal_branch
            ON deals(branch_id)
            WHERE branch_id IS NOT NULL
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_deal_delivery_org
            ON deals(organization_id, delivery_date)
            WHERE delivery_date IS NOT NULL AND is_deleted = FALSE
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_deal_org_deleted_status
            ON deals(organization_id, is_deleted, status)
        """))
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS ix_task_pending
            ON tasks(deal_id)
            WHERE status = 'PENDING'
        """))
    logger.info("Performance indexes verified / created")



# ── Entry point ───────────────────────────────────────────────────────────────

def run_all_migrations(engine: Engine) -> None:
    """
    Run every migration in dependency order.
    Called once during FastAPI lifespan startup.
    Exceptions are logged but do NOT abort startup — a running app with
    a missing column is better than a crashed app.
    """
    logger.info("Running database migrations…")
    try:
        migrate_branches_table(engine)   # must run before user/deal branch FKs
        migrate_deals_table(engine)
        migrate_user_branch(engine)
        migrate_deal_branch(engine)
        migrate_phone_constraint(engine) # replace full unique with partial (soft-delete safe)
        migrate_otp_verifications_table(engine)
        migrate_audit_logs_table(engine)
        migrate_indexes(engine)
        logger.info("All migrations completed successfully")
    except Exception:
        logger.exception("Migration error — app will continue, but schema may be incomplete")
