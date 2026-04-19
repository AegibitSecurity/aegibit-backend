"""
Excel Ingestion Service — production-grade dealership pricing sheet parser.

Handles:
  - Inconsistent header row positions (auto-detected)
  - Typos in column names (preimum → premium, accesories → accessories)
  - Missing BH columns (optional)
  - Duplicate "on road" vs "total" pricing (prefer total, fallback to on_road)
  - Extra/unnamed columns (dropped silently)
  - Structured diagnostics for every upload

Target Schema:
  variant, ex_showroom_price, tcs, insurance_premium,
  road_tax_5yr, road_tax_15yr, road_tax_bh,
  on_road_price_5yr, on_road_price_15yr, on_road_price_bh,
  extended_warranty, amc, accessories,
  total_5yr, total_15yr, total_bh

Architecture:
  detect_header_row() → normalize_columns() → map_columns() →
  validate_data() → transform_output()
"""

from __future__ import annotations

import re
import uuid
import logging
from io import BytesIO
from dataclasses import dataclass, field, asdict
from typing import Any

import pandas as pd
from thefuzz import fuzz
from sqlalchemy.orm import Session

from app.models import CarModel

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Model name extraction (extensible keyword → model mapping)
# ─────────────────────────────────────────────────────────────────────────────

MODEL_PREFIX_MAP: list[tuple[str, str]] = [
    ("punch",   "New Punch"),
    ("harrier", "Harrier"),
    ("nexon-p", "Nexon-P"),
    ("nexon-d", "Nexon-D"),
    ("nexon p", "Nexon-P"),
    ("nexon d", "Nexon-D"),
    ("nexon",   "Nexon"),
    ("tiago",   "Tiago"),
    ("tigor",   "Tigor"),
    ("sierra",  "Sierra"),
    ("safari",  "Safari"),
    ("altroz",  "Altroz"),
    ("curvv",   "Curvv"),
]


def extract_model_name(variant: str) -> str:
    """Derive model name from variant string using keyword matching + fallback."""
    variant_lower = variant.strip().lower()
    for keyword, model_name in MODEL_PREFIX_MAP:
        if keyword in variant_lower:
            return model_name
    first_word = variant_lower.split()[0] if variant_lower else "Unknown"
    return first_word.title()


# ─────────────────────────────────────────────────────────────────────────────
# Common typo corrections
# ─────────────────────────────────────────────────────────────────────────────

TYPO_MAP: dict[str, str] = {
    "preimum":      "premium",
    "prremium":     "premium",
    "premimum":     "premium",
    "accesories":   "accessories",
    "acessories":   "accessories",
    "accessries":   "accessories",
    "regsitration": "registration",
    "registation":  "registration",
    "registraion":  "registration",
    "regi":         "registration",
    "waranty":      "warranty",
    "warrnty":      "warranty",
    "warenty":      "warranty",
    "chg":          "charge",
    "rsa":          "rsa",
    "b2b":          "b2b",
}


# ─────────────────────────────────────────────────────────────────────────────
# Column mapping definitions
# ─────────────────────────────────────────────────────────────────────────────

# (internal_name, keyword_phrases_for_matching, required?)
COLUMN_DEFINITIONS: list[tuple[str, list[str], bool]] = [
    # ── Identity ──
    ("variant", [
        "variant", "model name", "car model", "vehicle name",
        "vehicle variant",
    ], True),

    # ── Core pricing ──
    ("ex_showroom_price", [
        "ex showroom price", "ex-showroom price", "ex showroom",
        "exshowroom price", "exshowroom", "esp",
    ], True),

    ("tcs", [
        "tcs 1", "tcs", "tax collected at source",
    ], False),

    ("insurance_premium", [
        "insurance premium", "insurance premium b2b rsa",
        "insurance premium b2b and rsa", "insurance",
    ], False),

    # ── Totals (MUST be matched BEFORE road_tax / on_road) ──
    ("total_5yr", [
        "total 5 years registration", "total 5 years regi",
        "total 5 year regi", "total 5yr regi",
        "total 5 years", "total 5yr",
    ], False),

    ("total_15yr", [
        "total 15 years registration", "total 15 years regi",
        "total 15 year regi", "total 15yr regi",
        "total 15 years", "total 15yr",
    ], False),

    ("total_bh", [
        "total bh registration", "total bh regi",
        "total bh", "bh registration total",
    ], False),

    # ── On-road prices (fallback if total is missing) ──
    ("on_road_price_5yr", [
        "on road price with 5 years registration",
        "on road price 5 years", "on road 5yr", "onroad price 5yr",
    ], False),

    ("on_road_price_15yr", [
        "on road price with 15 years registration",
        "on road price 15 years", "on road 15yr", "onroad price 15yr",
    ], False),

    ("on_road_price_bh", [
        "on road price with bh registration",
        "on road price bh", "on road bh", "onroad price bh",
    ], False),

    # ── Road tax / registration ──
    ("road_tax_5yr", [
        "5 years road tax registration", "5 years road tax and registration",
        "5 year road tax", "road tax 5 years", "road tax 5yr",
    ], False),

    ("road_tax_15yr", [
        "15 years road tax registration", "15 years road tax and registration",
        "15 year road tax", "road tax 15 years", "road tax 15yr",
    ], False),

    ("road_tax_bh", [
        "bh road tax registration", "bh road tax and registration charge",
        "bh road tax", "bh registration charge", "bh road tax charge",
    ], False),

    # ── Add-ons ──
    ("extended_warranty", [
        "extended warranty 2 years", "extended warranty",
        "warranty 2 years", "ext warranty",
    ], False),

    ("amc", [
        "amc", "annual maintenance contract", "annual maintenance",
    ], False),

    ("accessories", [
        "accessories pack", "accessories", "accessory pack",
        "accessory", "accessories package",
    ], False),
]

# Fuzzy match threshold (0–100)
FUZZY_THRESHOLD = 60


# ─────────────────────────────────────────────────────────────────────────────
# Diagnostics dataclass
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class IngestionDiagnostics:
    """Structured diagnostics returned with every ingestion."""
    header_row_index: int = -1
    raw_columns: list[str] = field(default_factory=list)
    cleaned_columns: list[str] = field(default_factory=list)
    column_mapping: dict[str, str] = field(default_factory=dict)
    unmatched_columns: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    typos_corrected: dict[str, str] = field(default_factory=dict)
    sample_rows: list[dict] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    rows_total: int = 0
    rows_inserted: int = 0
    rows_skipped: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


# ═════════════════════════════════════════════════════════════════════════════
# CORE PIPELINE FUNCTIONS
# ═════════════════════════════════════════════════════════════════════════════


def detect_header_row(file_bytes: bytes, max_scan: int = 25) -> int:
    """
    Auto-detect header row by scanning for rows containing pricing keywords.

    Strategy:
      1. Look for a row containing "variant" (strongest signal)
      2. Fallback: look for "ex showroom" or "ex-showroom"
      3. Final fallback: row 11 (common dealership format)

    Returns 0-based row index.
    """
    HEADER_SIGNALS = ["variant", "ex showroom", "ex-showroom", "model name"]

    try:
        df_raw = pd.read_excel(
            BytesIO(file_bytes), header=None, nrows=max_scan, dtype=str
        )
        for idx, row in df_raw.iterrows():
            row_text = " ".join(
                str(v).lower() for v in row.values
                if v is not None and str(v).strip()
            )
            # Check if this row has header signals
            if any(signal in row_text for signal in HEADER_SIGNALS):
                logger.info("Header auto-detected at row %d", idx)
                return int(idx)
    except Exception as exc:
        logger.warning("Header auto-detect failed: %s — fallback to row 11", exc)

    return 11


def normalize_columns(raw_columns: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    Normalize raw column headers:
      1. Lowercase
      2. Strip special characters
      3. Fix common typos
      4. Collapse whitespace

    Returns (cleaned_columns, typos_corrected_map).
    """
    cleaned = []
    typos_found: dict[str, str] = {}

    for raw in raw_columns:
        text = clean_column_name(raw)

        # Apply typo corrections
        corrected = text
        for typo, fix in TYPO_MAP.items():
            if typo in corrected:
                corrected = corrected.replace(typo, fix)
                if corrected != text:
                    typos_found[raw] = corrected

        cleaned.append(corrected)

    return cleaned, typos_found


def clean_column_name(raw: Any) -> str:
    """
    Normalize a single raw column header to a comparable string.

    Steps: stringify → lowercase → strip non-alphanumeric (keep spaces) →
    collapse whitespace → strip.
    """
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    text = str(raw).lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def map_columns(
    cleaned_columns: list[str],
    raw_columns: list[str],
) -> tuple[dict[str, str], list[str]]:
    """
    Map cleaned column names → internal field names using keyword-based rules.

    Uses AND/OR keyword presence checks for deterministic matching.
    Order matters: totals are matched BEFORE on_road to prevent mis-assignment
    when both columns exist in the same sheet.

    Returns (column_map, unmatched_columns).
    column_map: {internal_field: raw_column_name}
    """
    # (field_name, match_function)  — order is significant
    KEYWORD_RULES: list[tuple[str, Any]] = [
        # ── Identity ──
        ("variant",            lambda c: "variant" in c or "model name" in c
                                         or "car model" in c or "vehicle name" in c),
        # ── Core pricing ──
        ("ex_showroom_price",  lambda c: "ex showroom" in c or "exshowroom" in c
                                         or c.strip() == "esp" or c.startswith("esp")),
        ("tcs",                lambda c: "tcs" in c or "tax collected at source" in c),
        ("insurance_premium",  lambda c: "insurance" in c and "premium" in c),
        # ── Totals MUST match before on_road / road_tax ──
        ("total_5yr",          lambda c: "total" in c and ("5 year" in c or "5 years" in c)),
        ("total_15yr",         lambda c: "total" in c and ("15 year" in c or "15 years" in c)),
        ("total_bh",           lambda c: "total" in c and "bh" in c),
        # ── On-road prices (fallback) ──
        ("on_road_price_5yr",  lambda c: "on road price" in c and ("5 year" in c or "5 years" in c)),
        ("on_road_price_15yr", lambda c: "on road price" in c and ("15 year" in c or "15 years" in c
                                         or "lifetime" in c)),
        ("on_road_price_bh",   lambda c: "on road price" in c and "bh" in c),
        # ── Road tax / registration ──
        ("road_tax_5yr",       lambda c: "road tax" in c and ("5 year" in c or "5 years" in c)),
        ("road_tax_15yr",      lambda c: "road tax" in c and ("15 year" in c or "15 years" in c)),
        ("road_tax_bh",        lambda c: "road tax" in c and "bh" in c),
        # ── Add-ons ──
        ("extended_warranty",  lambda c: "extended warranty" in c
                                         or ("warranty" in c and "2 year" in c)),
        ("amc",                lambda c: c.strip() == "amc" or "annual maintenance" in c),
        ("accessories",        lambda c: "accessories" in c or "accessory" in c),
    ]

    column_map: dict[str, str] = {}
    matched_indices: set[int] = set()

    for field_name, match_fn in KEYWORD_RULES:
        for i, cleaned in enumerate(cleaned_columns):
            if i in matched_indices or not cleaned:
                continue
            if match_fn(cleaned):
                column_map[field_name] = raw_columns[i]
                matched_indices.add(i)
                break  # first column match wins for this field

    unmatched = [
        raw_columns[i] for i in range(len(raw_columns))
        if i not in matched_indices and raw_columns[i] and str(raw_columns[i]).strip()
    ]
    return column_map, unmatched


# Keep old name as alias for test compatibility
def fuzzy_map_columns(raw_columns: list[str]) -> tuple[dict[str, str], list[str]]:
    """Backward-compatible wrapper around normalize + map."""
    cleaned, _ = normalize_columns(raw_columns)
    return map_columns(cleaned, raw_columns)


def validate_data(
    column_map: dict[str, str],
) -> list[str]:
    """
    Validate that required columns are present.

    Rules:
      - variant: REQUIRED
      - ex_showroom_price: REQUIRED
      - At least one of: total_5yr OR on_road_price_5yr
      - At least one of: total_15yr OR on_road_price_15yr
      - total_bh / on_road_price_bh: OPTIONAL

    Returns list of validation errors (empty = valid).
    """
    errors: list[str] = []
    mapped = set(column_map.keys())

    # Hard required
    if "variant" not in mapped:
        errors.append("Missing required column: variant")
    if "ex_showroom_price" not in mapped:
        errors.append("Missing required column: ex_showroom_price")

    # Conditional required — need total OR on_road for 5yr and 15yr
    if "total_5yr" not in mapped and "on_road_price_5yr" not in mapped:
        errors.append(
            "Missing pricing for 5-year: need 'Total (5 Years)' or "
            "'On Road Price with 5 Years Registration'"
        )
    if "total_15yr" not in mapped and "on_road_price_15yr" not in mapped:
        errors.append(
            "Missing pricing for 15-year: need 'Total (15 Years)' or "
            "'On Road Price with 15 Years Registration'"
        )

    return errors


def transform_output(
    row: pd.Series,
    column_map: dict[str, str],
) -> dict[str, Any] | None:
    """
    Transform a single DataFrame row into a clean output dict.

    Applies fallback logic:
      - If total_* exists → use it
      - Else fallback to on_road_price_*

    Returns None if the row should be skipped (empty variant / missing prices).
    """
    # ── Variant ──
    variant_raw = _get_mapped_value(row, column_map, "variant")
    if variant_raw is None or (isinstance(variant_raw, float) and pd.isna(variant_raw)):
        return None
    variant = str(variant_raw).strip()
    if not variant or variant.lower() in ("nan", "-", ""):
        return None

    # ── Ex-showroom (required) ──
    ex_showroom = _to_float(_get_mapped_value(row, column_map, "ex_showroom_price"))

    # ── Parse all optional price columns ──
    tcs = _to_float(_get_mapped_value(row, column_map, "tcs"))
    insurance = _to_float(_get_mapped_value(row, column_map, "insurance_premium"))
    road_tax_5yr = _to_float(_get_mapped_value(row, column_map, "road_tax_5yr"))
    road_tax_15yr = _to_float(_get_mapped_value(row, column_map, "road_tax_15yr"))
    road_tax_bh = _to_float(_get_mapped_value(row, column_map, "road_tax_bh"))
    on_road_5yr = _to_float(_get_mapped_value(row, column_map, "on_road_price_5yr"))
    on_road_15yr = _to_float(_get_mapped_value(row, column_map, "on_road_price_15yr"))
    on_road_bh = _to_float(_get_mapped_value(row, column_map, "on_road_price_bh"))
    warranty = _to_float(_get_mapped_value(row, column_map, "extended_warranty"))
    amc = _to_float(_get_mapped_value(row, column_map, "amc"))
    accessories = _to_float(_get_mapped_value(row, column_map, "accessories"))
    total_5yr = _to_float(_get_mapped_value(row, column_map, "total_5yr"))
    total_15yr = _to_float(_get_mapped_value(row, column_map, "total_15yr"))
    total_bh = _to_float(_get_mapped_value(row, column_map, "total_bh"))

    # ── Fallback: prefer total_*, else use on_road_price_* ──
    effective_5yr = total_5yr if total_5yr is not None else on_road_5yr
    effective_15yr = total_15yr if total_15yr is not None else on_road_15yr
    effective_bh = total_bh if total_bh is not None else on_road_bh

    # ── Must have at least 5yr or 15yr pricing ──
    if effective_5yr is None and effective_15yr is None:
        return None

    # ── Ex-showroom fallback ──
    if ex_showroom is None:
        ex_showroom = effective_5yr or effective_15yr

    return {
        "variant": variant,
        "ex_showroom_price": ex_showroom,
        "tcs": tcs,
        "insurance_premium": insurance,
        "road_tax_5yr": road_tax_5yr,
        "road_tax_15yr": road_tax_15yr,
        "road_tax_bh": road_tax_bh,
        "on_road_price_5yr": on_road_5yr,
        "on_road_price_15yr": on_road_15yr,
        "on_road_price_bh": on_road_bh,
        "extended_warranty": warranty,
        "amc": amc,
        "accessories": accessories,
        "total_5yr": effective_5yr,
        "total_15yr": effective_15yr,
        "total_bh": effective_bh,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_mapped_value(row: pd.Series, column_map: dict[str, str], field: str) -> Any:
    """Safely get a value from a row using the column map."""
    raw_col = column_map.get(field)
    if raw_col is None:
        return None
    return row.get(raw_col)


def _to_float(val: Any) -> float | None:
    """Coerce a cell value to float. Returns None for blanks/dashes/non-numeric."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if pd.isna(val):
            return None
        return float(val)
    s = str(val).strip().replace(",", "").replace("₹", "").replace("$", "")
    if not s or s == "-" or s.lower() == "nan":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# Keep old alias for test compatibility
def auto_detect_header_row(file_bytes: bytes, max_scan: int = 25) -> int:
    """Backward-compatible alias for detect_header_row."""
    return detect_header_row(file_bytes, max_scan)


# ═════════════════════════════════════════════════════════════════════════════
# PUBLIC ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def ingest_excel(
    file_bytes: bytes,
    organization_id: str,
    db: Session,
) -> dict[str, Any]:
    """
    Parse a dealership Excel pricing sheet and insert CarModel rows.

    Returns dict with:
      rows_inserted, rows_skipped, errors, upload_batch, diagnostics
    """
    diag = IngestionDiagnostics()
    errors: list[str] = []

    # ── 1. Detect header row ──────────────────────────────────────────────────
    header_idx = detect_header_row(file_bytes)
    diag.header_row_index = header_idx

    # ── 2. Read Excel ─────────────────────────────────────────────────────────
    try:
        df = pd.read_excel(BytesIO(file_bytes), header=header_idx)
    except Exception as exc:
        errors.append(f"Failed to read Excel file: {exc}")
        return _build_result(0, 0, errors, "", diag)

    if df.empty:
        errors.append("Excel file has no data rows after the header.")
        return _build_result(0, 0, errors, "", diag)

    # Drop completely empty / unnamed columns
    df = df.loc[:, ~df.columns.str.contains('^Unnamed', na=False)]
    df = df.dropna(how='all')

    # ── 3. Normalize column names ─────────────────────────────────────────────
    raw_columns = [str(c) for c in df.columns]
    diag.raw_columns = raw_columns

    cleaned_columns, typos = normalize_columns(raw_columns)
    diag.cleaned_columns = cleaned_columns
    diag.typos_corrected = typos

    logger.info("Raw columns: %s", raw_columns)
    if typos:
        logger.info("Typos corrected: %s", typos)

    # ── 4. Map columns ────────────────────────────────────────────────────────
    column_map, unmatched = map_columns(cleaned_columns, raw_columns)
    diag.column_mapping = column_map
    diag.unmatched_columns = unmatched

    logger.info("Column mapping: %s", column_map)

    # ── 5. Validate required columns ──────────────────────────────────────────
    validation_errors = validate_data(column_map)
    if validation_errors:
        diag.validation_errors = validation_errors
        for ve in validation_errors:
            errors.append(ve)
        return _build_result(0, 0, errors, "", diag)

    diag.missing_fields = [
        name for name, _, _ in COLUMN_DEFINITIONS
        if name not in column_map
    ]

    # ── 6. Process rows ───────────────────────────────────────────────────────
    upload_batch = str(uuid.uuid4())
    rows_inserted = 0
    rows_skipped = 0
    diag.rows_total = len(df)

    for pandas_idx, row in df.iterrows():
        excel_row = header_idx + 2 + int(pandas_idx)

        try:
            parsed = transform_output(row, column_map)
            if parsed is None:
                rows_skipped += 1
                continue

            # Collect sample rows (first 3)
            if len(diag.sample_rows) < 3:
                diag.sample_rows.append(parsed)

            # ── Insert into DB ────────────────────────────────────────────────
            model_name = extract_model_name(parsed["variant"])
            db.add(CarModel(
                organization_id=organization_id,
                variant=parsed["variant"].lower(),
                model_name=model_name,
                ex_showroom_price=parsed["ex_showroom_price"],
                total_5yr=parsed["total_5yr"],
                total_15yr=parsed["total_15yr"],
                total_bh=parsed["total_bh"],
                upload_batch=upload_batch,
                is_active=True,
            ))
            rows_inserted += 1

        except Exception as exc:
            rows_skipped += 1
            errors.append(f"Row {excel_row}: unexpected error — {exc}")

    # ── 7. Batch swap ─────────────────────────────────────────────────────────
    if rows_inserted > 0:
        db.query(CarModel).filter(
            CarModel.organization_id == organization_id,
            CarModel.upload_batch != upload_batch,
            CarModel.is_active == True,
        ).update({"is_active": False})
        db.commit()
        logger.info(
            "Ingestion complete: %d inserted, %d skipped, %d errors",
            rows_inserted, rows_skipped, len(errors),
        )
    else:
        db.rollback()
        if not errors:
            errors.append(
                "No valid data rows found. Ensure the spreadsheet contains "
                "variant names and numeric price values."
            )

    diag.rows_inserted = rows_inserted
    diag.rows_skipped = rows_skipped

    return _build_result(rows_inserted, rows_skipped, errors, upload_batch, diag)


def _build_result(
    inserted: int,
    skipped: int,
    errors: list[str],
    batch: str,
    diag: IngestionDiagnostics,
) -> dict[str, Any]:
    """Build the standard ingestion result dict."""
    return {
        "rows_inserted": inserted,
        "rows_skipped": skipped,
        "errors": errors,
        "upload_batch": batch,
        "diagnostics": diag.to_dict(),
    }


def _empty_result(errors: list[str]) -> dict[str, Any]:
    """Backward-compatible empty result."""
    return {
        "rows_inserted": 0,
        "rows_skipped": 0,
        "errors": errors,
        "upload_batch": "",
    }
