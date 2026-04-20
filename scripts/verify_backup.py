"""
verify_backup.py — Restore latest backup to a throwaway DB and verify integrity.

Run this weekly (e.g. Render Cron on Sunday) to confirm backups are restorable.

Required env vars:
  DATABASE_URL              production DB (source of truth row counts)
  VERIFY_DATABASE_URL       temp/test DB to restore into (separate from prod!)
  S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  S3_ENDPOINT_URL           (optional)
  ALERT_WEBHOOK_URL         (optional) Slack/Teams webhook

Usage:
  python scripts/verify_backup.py
"""

import logging
import os
import subprocess
import sys
import tempfile
import urllib.request
import json
import gzip
from urllib.parse import urlparse

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BUCKET          = os.environ["S3_BUCKET_NAME"]
ENDPOINT_URL    = os.getenv("S3_ENDPOINT_URL")
VERIFY_DB_URL   = os.environ["VERIFY_DATABASE_URL"]
PROD_DB_URL     = os.environ["DATABASE_URL"]
WEBHOOK_URL     = os.getenv("ALERT_WEBHOOK_URL")

CRITICAL_TABLES = ["deals", "audit_logs", "organizations", "users"]


def _s3():
    kwargs = {"endpoint_url": ENDPOINT_URL} if ENDPOINT_URL else {}
    return boto3.client("s3", **kwargs)


def _latest_key() -> str:
    s3 = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    items = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix="backups/"):
        for obj in page.get("Contents", []):
            items.append((obj["LastModified"], obj["Key"]))
    if not items:
        raise RuntimeError("No backups found in S3 bucket")
    items.sort(reverse=True)
    return items[0][1]


def _psql(db_url: str, sql: str) -> str:
    parsed = urlparse(db_url)
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""
    cmd = [
        "psql",
        f"--host={parsed.hostname}",
        f"--port={parsed.port or 5432}",
        f"--username={parsed.username}",
        "--tuples-only",
        "--command", sql,
        parsed.path.lstrip("/"),
    ]
    result = subprocess.run(cmd, env=env, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql error: {result.stderr.decode()}")
    return result.stdout.decode().strip()


def _row_count(db_url: str, table: str) -> int:
    out = _psql(db_url, f"SELECT COUNT(*) FROM {table};")
    try:
        return int(out.split()[-1])
    except (ValueError, IndexError):
        return -1


def _restore_to_verify_db(dump_path: str):
    parsed = urlparse(VERIFY_DB_URL)
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""
    logger.info("Restoring to verify DB: %s@%s/%s",
                parsed.username, parsed.hostname, parsed.path.lstrip("/"))
    with gzip.open(dump_path, "rb") as gz:
        sql = gz.read()
    cmd = [
        "psql",
        f"--host={parsed.hostname}",
        f"--port={parsed.port or 5432}",
        f"--username={parsed.username}",
        parsed.path.lstrip("/"),
    ]
    result = subprocess.run(cmd, env=env, input=sql, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"Restore failed: {result.stderr.decode()}")
    logger.info("Restore to verify DB complete")


def _alert(message: str):
    if not WEBHOOK_URL:
        return
    try:
        body = json.dumps({"text": message}).encode()
        req = urllib.request.Request(WEBHOOK_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logger.warning("Alert failed: %s", exc)


def main():
    logger.info("=== Backup Verification Started ===")

    key = _latest_key()
    logger.info("Latest backup key: %s", key)

    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        s3 = _s3()
        logger.info("Downloading %s …", key)
        s3.download_file(BUCKET, key, tmp_path)

        _restore_to_verify_db(tmp_path)

        failures = []
        for table in CRITICAL_TABLES:
            try:
                prod_count   = _row_count(PROD_DB_URL,   table)
                verify_count = _row_count(VERIFY_DB_URL, table)
                logger.info("Table %-20s  prod=%d  verify=%d", table, prod_count, verify_count)
                if verify_count < prod_count * 0.95:
                    failures.append(f"{table}: prod={prod_count} verify={verify_count} (>5% loss)")
            except Exception as exc:
                failures.append(f"{table}: {exc}")

        if failures:
            msg = "🚨 AEGIBIT backup verification FAILED:\n" + "\n".join(failures)
            logger.error(msg)
            _alert(msg)
            sys.exit(1)
        else:
            msg = f"✅ AEGIBIT backup verified OK — key: `{key}`"
            logger.info(msg)
            _alert(msg)

    except Exception as exc:
        logger.error("Verification error: %s", exc)
        _alert(f"🚨 AEGIBIT backup verification ERROR: {exc}")
        sys.exit(1)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    logger.info("=== Backup Verification Complete ===")


if __name__ == "__main__":
    main()
