"""
apply_hardening.py — Apply db_hardening.sql to the production PostgreSQL database.

Run once during initial setup or after a database reset.
Safe to re-run: all objects use CREATE OR REPLACE / DROP IF EXISTS.

Usage:
  DATABASE_URL=postgresql://... python scripts/apply_hardening.py
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
SQL_FILE     = Path(__file__).parent / "db_hardening.sql"


def main():
    if not DATABASE_URL:
        print("ERROR: DATABASE_URL environment variable is required")
        sys.exit(1)

    if "sqlite" in DATABASE_URL:
        print("SKIP: db_hardening.sql is PostgreSQL-only. SQLite detected — skipping.")
        return

    if not SQL_FILE.exists():
        print(f"ERROR: {SQL_FILE} not found")
        sys.exit(1)

    logger.info("Applying DB hardening from %s …", SQL_FILE)

    from urllib.parse import urlparse
    parsed = urlparse(DATABASE_URL)
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""

    cmd = [
        "psql",
        f"--host={parsed.hostname}",
        f"--port={parsed.port or 5432}",
        f"--username={parsed.username}",
        f"--file={SQL_FILE}",
        parsed.path.lstrip("/"),
    ]

    result = subprocess.run(cmd, env=env, capture_output=True)
    if result.returncode != 0:
        logger.error("psql error:\n%s", result.stderr.decode())
        sys.exit(1)

    logger.info("DB hardening applied successfully")
    if result.stdout:
        logger.info(result.stdout.decode())


if __name__ == "__main__":
    main()
