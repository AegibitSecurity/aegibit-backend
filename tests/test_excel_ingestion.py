"""
Tests for Excel ingestion service.

Covers:
  - Column fuzzy matching
  - Header auto-detection
  - Row parsing and validation
  - Batch swap logic
  - Error handling for missing columns / bad data
"""

import pytest
import pandas as pd
from io import BytesIO

from app.services.excel_ingestion import (
    clean_column_name,
    fuzzy_map_columns,
    auto_detect_header_row,
    ingest_excel,
    COLUMN_DEFINITIONS,
)


# ─────────────────────────────────────────────────────────────────────────────
# clean_column_name
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanColumnName:

    def test_basic_normalization(self):
        assert clean_column_name("Ex-Showroom Price") == "ex showroom price"

    def test_removes_special_chars(self):
        assert clean_column_name("Total (5 Years Regi)") == "total 5 years regi"

    def test_collapses_whitespace(self):
        assert clean_column_name("  variant   name  ") == "variant name"

    def test_none_returns_empty(self):
        assert clean_column_name(None) == ""

    def test_nan_returns_empty(self):
        assert clean_column_name(float("nan")) == ""

    def test_numeric_column(self):
        assert clean_column_name(12345) == "12345"


# ─────────────────────────────────────────────────────────────────────────────
# fuzzy_map_columns
# ─────────────────────────────────────────────────────────────────────────────

class TestFuzzyMapColumns:

    def test_exact_match(self):
        raw = ["Variant", "Ex Showroom Price", "Total (5 Years Regi)", "Total (15 Years Regi)", "Total (BH Regi)"]
        col_map, unmatched = fuzzy_map_columns(raw)
        assert "variant" in col_map
        assert "total_5yr" in col_map
        assert "total_15yr" in col_map
        assert "total_bh" in col_map

    def test_fuzzy_match_variant_names(self):
        raw = ["Car Model", "ExShowroom", "Total 5yr", "Total 15yr", "BH Registration"]
        col_map, unmatched = fuzzy_map_columns(raw)
        assert "variant" in col_map
        assert "total_5yr" in col_map

    def test_unmatched_columns_returned(self):
        raw = ["Variant", "Total (5 Years Regi)", "Total (15 Years Regi)", "Total (BH Regi)", "Color", "Weight"]
        col_map, unmatched = fuzzy_map_columns(raw)
        assert "Color" in unmatched or "Weight" in unmatched

    def test_missing_required_detected(self):
        raw = ["Variant", "Some Random Column"]
        col_map, _ = fuzzy_map_columns(raw)
        # Should be missing ex_showroom_price at minimum
        assert "ex_showroom_price" not in col_map

    def test_empty_columns_handled(self):
        raw = ["", None, "Variant"]
        col_map, _ = fuzzy_map_columns(raw)
        assert "variant" in col_map


# ─────────────────────────────────────────────────────────────────────────────
# auto_detect_header_row
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoDetectHeader:

    def _make_excel(self, rows):
        """Create an Excel file in memory with given rows."""
        df = pd.DataFrame(rows)
        buf = BytesIO()
        df.to_excel(buf, index=False, header=False)
        return buf.getvalue()

    def test_detects_variant_in_header_row(self):
        rows = [
            ["Title Row", "", ""],
            ["", "", ""],
            ["Variant", "Price", "Total"],
            ["Pulsar", 150000, 170000],
        ]
        idx = auto_detect_header_row(self._make_excel(rows))
        assert idx == 2  # 0-based

    def test_detects_header_at_row_zero(self):
        rows = [
            ["Variant", "Total 5yr", "Total 15yr"],
            ["Model A", 100000, 110000],
        ]
        idx = auto_detect_header_row(self._make_excel(rows))
        assert idx == 0

    def test_fallback_when_no_variant_found(self):
        rows = [
            ["Column A", "Column B"],
            ["data", "data"],
        ]
        idx = auto_detect_header_row(self._make_excel(rows))
        assert idx == 11  # default fallback


# ─────────────────────────────────────────────────────────────────────────────
# ingest_excel (integration with DB)
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestExcel:

    def _make_pricing_excel(self, data_rows, header=None):
        """Create a well-formed pricing Excel."""
        if header is None:
            header = ["Variant", "Ex Showroom Price", "Total (5 Years Regi)", "Total (15 Years Regi)", "Total (BH Regi)"]
        df = pd.DataFrame(data_rows, columns=header)
        buf = BytesIO()
        df.to_excel(buf, index=False)
        return buf.getvalue()

    def test_successful_ingestion(self, db_session, org):
        content = self._make_pricing_excel([
            ["Pulsar NS200", 150000, 170000, 175000, 180000],
            ["Dominar 400", 220000, 250000, 260000, 270000],
        ])
        result = ingest_excel(content, org.id, db_session)
        assert result["rows_inserted"] == 2
        assert result["rows_skipped"] == 0
        assert len(result["errors"]) == 0
        assert result["upload_batch"] != ""

    def test_skips_rows_with_missing_prices(self, db_session, org):
        content = self._make_pricing_excel([
            ["Pulsar NS200", 150000, 170000, 175000, 180000],
            ["Bad Model", None, None, None, None],
        ])
        result = ingest_excel(content, org.id, db_session)
        assert result["rows_inserted"] == 1
        assert result["rows_skipped"] == 1

    def test_skips_empty_variant_rows(self, db_session, org):
        content = self._make_pricing_excel([
            ["", 150000, 170000, 175000, 180000],
            ["Pulsar NS200", 150000, 170000, 175000, 180000],
        ])
        result = ingest_excel(content, org.id, db_session)
        assert result["rows_inserted"] == 1
        assert result["rows_skipped"] == 1

    def test_missing_required_columns_fails(self, db_session, org):
        content = self._make_pricing_excel(
            [["Model A", 100]],
            header=["Variant", "Random Price"],
        )
        result = ingest_excel(content, org.id, db_session)
        assert result["rows_inserted"] == 0
        assert len(result["errors"]) > 0
        assert "Missing" in result["errors"][0]

    def test_batch_swap_deactivates_old(self, db_session, org):
        from app.models import CarModel

        # First batch
        content1 = self._make_pricing_excel([
            ["Model A", 100000, 110000, 115000, 120000],
        ])
        result1 = ingest_excel(content1, org.id, db_session)
        assert result1["rows_inserted"] == 1

        batch1 = result1["upload_batch"]

        # Second batch
        content2 = self._make_pricing_excel([
            ["Model B", 200000, 210000, 215000, 220000],
        ])
        result2 = ingest_excel(content2, org.id, db_session)
        assert result2["rows_inserted"] == 1

        # Old batch should be deactivated
        old = db_session.query(CarModel).filter(
            CarModel.upload_batch == batch1,
            CarModel.is_active == True,
        ).count()
        assert old == 0

        # New batch should be active
        new = db_session.query(CarModel).filter(
            CarModel.upload_batch == result2["upload_batch"],
            CarModel.is_active == True,
        ).count()
        assert new == 1

    def test_variant_stored_lowercase(self, db_session, org):
        from app.models import CarModel

        content = self._make_pricing_excel([
            ["Pulsar NS200", 150000, 170000, 175000, 180000],
        ])
        ingest_excel(content, org.id, db_session)
        car = db_session.query(CarModel).filter(
            CarModel.organization_id == org.id,
            CarModel.is_active == True,
        ).first()
        assert car.variant == "pulsar ns200"

    def test_empty_file_returns_error(self, db_session, org):
        df = pd.DataFrame()
        buf = BytesIO()
        df.to_excel(buf, index=False)
        result = ingest_excel(buf.getvalue(), org.id, db_session)
        assert result["rows_inserted"] == 0
        assert len(result["errors"]) > 0
