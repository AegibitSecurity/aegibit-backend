"""
Upload routes — Excel pricing upload.

Endpoints
---------
POST /upload-pricing       Original endpoint (kept for backward compat)
POST /upload-excel         New endpoint with debug info
POST /upload-preview       Preview first rows + detected columns (no DB writes)

Role access:
  upload-pricing  → GM+
  upload-excel    → GM+
  upload-preview  → SALES+ (read-only)
"""

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import AuthContext, get_current_user, require_role, Role
from app.schemas import UploadResponse
from app.services.excel_ingestion import (
    ingest_excel,
    auto_detect_header_row,
    fuzzy_map_columns,
    clean_column_name,
)
from app.services.notification_service import notify_pricing_uploaded

import pandas as pd
from io import BytesIO
import logging

logger = logging.getLogger(__name__)

router = APIRouter(tags=["upload"])


# ── Validation ────────────────────────────────────────────────────────────────

def _validate_file(file: UploadFile):
    """Check file extension."""
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xls")):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx or .xls files are accepted.",
        )


# ── POST /upload-pricing  (original — backward compatible) ───────────────────

@router.post("/upload-pricing", response_model=UploadResponse)
async def upload_pricing(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(require_role(Role.GM)),
    db: Session = Depends(get_db),
):
    _validate_file(file)
    content = await file.read()
    result = ingest_excel(content, auth.org_id, db)

    if result["rows_inserted"] == 0 and result["errors"]:
        raise HTTPException(status_code=422, detail=result["errors"])

    # Fire notification on successful upload
    if result["rows_inserted"] > 0:
        await notify_pricing_uploaded(
            db, auth.org_id,
            inserted=result["rows_inserted"],
            skipped=result["rows_skipped"],
            batch_id=result["upload_batch"],
        )

    return UploadResponse(**result)


# ── POST /upload-excel  (new — returns extra debug info) ─────────────────────

@router.post("/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(require_role(Role.GM)),
    db: Session = Depends(get_db),
):
    """
    Enhanced upload endpoint that returns the ingestion result plus
    diagnostic metadata (detected header row, column mapping, sample rows).
    """
    _validate_file(file)
    content = await file.read()

    # Detect header for diagnostics
    header_idx = auto_detect_header_row(content)

    # Run the ingestion
    result = ingest_excel(content, auth.org_id, db)

    # Build diagnostics (best-effort)
    diagnostics = {"header_row_0based": header_idx}
    try:
        df = pd.read_excel(BytesIO(content), header=header_idx, nrows=5)
        raw_cols = [str(c) for c in df.columns]
        col_map, unmatched = fuzzy_map_columns(raw_cols)
        diagnostics["raw_columns"] = raw_cols
        diagnostics["column_mapping"] = col_map
        diagnostics["unmatched_columns"] = unmatched
        diagnostics["sample_rows"] = df.head(3).fillna("").to_dict(orient="records")
    except Exception as exc:
        diagnostics["diagnostics_error"] = str(exc)

    if result["rows_inserted"] == 0 and result["errors"]:
        raise HTTPException(
            status_code=422,
            detail={
                "errors": result["errors"],
                "diagnostics": diagnostics,
            },
        )

    # Fire notification on successful upload
    if result["rows_inserted"] > 0:
        await notify_pricing_uploaded(
            db, auth.org_id,
            inserted=result["rows_inserted"],
            skipped=result["rows_skipped"],
            batch_id=result["upload_batch"],
        )

    return {**result, "diagnostics": diagnostics}


# ── POST /upload-preview  (dry-run, no DB writes) ─────────────────────────────

@router.post("/upload-preview")
async def upload_preview(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_user),
):
    """
    Preview the Excel file: auto-detect header, show column mapping,
    and return the first 5 data rows — without writing anything to the DB.
    """
    _validate_file(file)
    content = await file.read()

    header_idx = auto_detect_header_row(content)

    try:
        df = pd.read_excel(BytesIO(content), header=header_idx, nrows=10)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Cannot read Excel: {exc}")

    raw_cols = [str(c) for c in df.columns]
    col_map, unmatched = fuzzy_map_columns(raw_cols)

    return {
        "header_row_0based": header_idx,
        "header_row_1based": header_idx + 1,
        "raw_columns": raw_cols,
        "column_mapping": col_map,
        "unmatched_columns": unmatched,
        "sample_rows": df.head(5).fillna("").to_dict(orient="records"),
        "total_preview_rows": len(df),
    }
