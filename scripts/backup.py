"""
backup.py — pg_dump → gzip → S3-compatible upload + retention cleanup.

Required environment variables:
  DATABASE_URL          postgresql://user:pass@host:5432/dbname
  S3_BUCKET_NAME        e.g. aegibit-backups
  AWS_ACCESS_KEY_ID     IAM or R2 / B2 key
  AWS_SECRET_ACCESS_KEY IAM or R2 / B2 secret
  S3_ENDPOINT_URL       (optional) for Cloudflare R2 / Backblaze B2
  BACKUP_RETENTION_DAYS (optional, default 30)
  ALERT_WEBHOOK_URL     (optional) Slack / Teams webhook for failure alerts

Usage:
  python scripts/backup.py

Render Cron: set command to `python scripts/backup.py`, schedule daily.
"""

import gzip
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BUCKET        = os.environ["S3_BUCKET_NAME"]
RETENTION     = int(os.getenv("BACKUP_RETENTION_DAYS", "30"))
ENDPOINT_URL  = os.getenv("S3_ENDPOINT_URL")          # None → AWS S3
WEBHOOK_URL   = os.getenv("ALERT_WEBHOOK_URL")
DATABASE_URL  = os.environ["DATABASE_URL"]


def _s3_client():
    kwargs = {}
    if ENDPOINT_URL:
        kwargs["endpoint_url"] = ENDPOINT_URL
    return boto3.client("s3", **kwargs)


def _pg_dump(db_url: str, out_path: str):
    """Run pg_dump, writing compressed SQL to out_path."""
    parsed = urlparse(db_url)
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""

    cmd = [
        "pg_dump",
        "--format=plain",
        "--no-owner",
        "--no-acl",
        f"--host={parsed.hostname}",
        f"--port={parsed.port or 5432}",
        f"--username={parsed.username}",
        parsed.path.lstrip("/"),
    ]
    logger.info("Running pg_dump…")
    with open(out_path, "wb") as f:
        result = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {result.stderr.decode()}")
        f.write(gzip.compress(result.stdout))
    size_mb = os.path.getsize(out_path) / 1_048_576
    logger.info("Dump complete — %.1f MB compressed", size_mb)


def _upload(local_path: str, s3_key: str):
    s3 = _s3_client()
    logger.info("Uploading to s3://%s/%s", BUCKET, s3_key)
    s3.upload_file(local_path, BUCKET, s3_key)
    logger.info("Upload complete")


def _purge_old_backups():
    """Delete backups older than RETENTION days."""
    s3 = _s3_client()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION)
    paginator = s3.get_paginator("list_objects_v2")
    deleted = 0
    for page in paginator.paginate(Bucket=BUCKET, Prefix="backups/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                s3.delete_object(Bucket=BUCKET, Key=obj["Key"])
                logger.info("Deleted old backup: %s", obj["Key"])
                deleted += 1
    logger.info("Purged %d old backup(s)", deleted)


def _alert(message: str):
    if not WEBHOOK_URL:
        return
    try:
        import urllib.request, json
        body = json.dumps({"text": message}).encode()
        req = urllib.request.Request(WEBHOOK_URL, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:
        logger.warning("Alert webhook failed: %s", exc)


def main():
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3_key = f"backups/{ts}-aegibit.sql.gz"

    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _pg_dump(DATABASE_URL, tmp_path)
        _upload(tmp_path, s3_key)
        _purge_old_backups()
        # Stamp the time so /admin/backup/status can report it
        _alert(f"✅ AEGIBIT backup succeeded: `{s3_key}`")
        logger.info("Backup job complete — key: %s", s3_key)
        # Write timestamp to a file the app can read if on the same filesystem
        try:
            with open("/tmp/last_backup_at.txt", "w") as f:
                f.write(datetime.now(timezone.utc).isoformat())
        except Exception:
            pass
    except Exception as exc:
        logger.error("Backup failed: %s", exc)
        _alert(f"🚨 AEGIBIT backup FAILED: {exc}")
        sys.exit(1)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
