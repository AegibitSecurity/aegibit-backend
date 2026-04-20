"""
restore.py — Download a backup from S3 and restore it to a PostgreSQL database.

Usage:
  # List available backups:
  python scripts/restore.py --list

  # Restore the most recent backup to a test DB (safe — doesn't touch prod):
  python scripts/restore.py --latest --target postgresql://user:pass@host/testdb

  # Restore a specific backup:
  python scripts/restore.py --key backups/20260420T030000Z-aegibit.sql.gz \
                             --target postgresql://user:pass@host/testdb

  # Restore to prod (requires --confirm-prod flag):
  python scripts/restore.py --latest --target $DATABASE_URL --confirm-prod

Required env vars:
  S3_BUCKET_NAME, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
  S3_ENDPOINT_URL  (optional, for R2/B2)
"""

import argparse
import gzip
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from urllib.parse import urlparse

import boto3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BUCKET       = os.environ["S3_BUCKET_NAME"]
ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")


def _s3():
    kwargs = {"endpoint_url": ENDPOINT_URL} if ENDPOINT_URL else {}
    return boto3.client("s3", **kwargs)


def list_backups():
    s3 = _s3()
    paginator = s3.get_paginator("list_objects_v2")
    items = []
    for page in paginator.paginate(Bucket=BUCKET, Prefix="backups/"):
        for obj in page.get("Contents", []):
            items.append((obj["LastModified"], obj["Key"], obj["Size"]))
    items.sort(reverse=True)
    print(f"\n{'Date (UTC)':<28} {'Size MB':>8}  Key")
    print("-" * 80)
    for last_mod, key, size in items:
        print(f"{last_mod.strftime('%Y-%m-%d %H:%M:%S UTC'):<28} {size/1_048_576:>8.1f}  {key}")
    print()
    return items


def _latest_key() -> str:
    items = list_backups()
    if not items:
        raise RuntimeError("No backups found in bucket")
    return items[0][1]


def _download(s3_key: str, out_path: str):
    s3 = _s3()
    logger.info("Downloading s3://%s/%s", BUCKET, s3_key)
    s3.download_file(BUCKET, s3_key, out_path)
    logger.info("Download complete")


def _restore(dump_path: str, target_url: str):
    parsed = urlparse(target_url)
    env = os.environ.copy()
    env["PGPASSWORD"] = parsed.password or ""

    logger.info("Restoring to %s@%s/%s …",
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
        raise RuntimeError(f"psql failed: {result.stderr.decode()}")
    logger.info("Restore complete")


def main():
    parser = argparse.ArgumentParser(description="Restore AEGIBIT backup from S3")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="List available backups and exit")
    group.add_argument("--latest", action="store_true", help="Use the most recent backup")
    group.add_argument("--key", help="Specific S3 key to restore")
    parser.add_argument("--target", help="Target PostgreSQL URL")
    parser.add_argument("--confirm-prod", action="store_true",
                        help="Required when targeting production DATABASE_URL")
    args = parser.parse_args()

    if args.list:
        list_backups()
        return

    if not args.target:
        parser.error("--target is required")

    prod_url = os.getenv("DATABASE_URL", "")
    if args.target == prod_url and not args.confirm_prod:
        print("ERROR: Restoring to production requires --confirm-prod flag.")
        print("       Double-check your --target URL before proceeding.")
        sys.exit(1)

    s3_key = _latest_key() if args.latest else args.key
    if not s3_key:
        parser.error("Provide --latest or --key")

    with tempfile.NamedTemporaryFile(suffix=".sql.gz", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _download(s3_key, tmp_path)
        _restore(tmp_path, args.target)
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


if __name__ == "__main__":
    main()
