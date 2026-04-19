"""
Tests for model grouping, customer details, and full deal flow.
"""

import pytest
import pandas as pd
from io import BytesIO

from app.services.excel_ingestion import extract_model_name, ingest_excel
from tests.conftest import make_headers


# ── Model name extraction ─────────────────────────────────────────────────────

class TestModelExtraction:

    def test_punch_variant(self):
        assert extract_model_name("Punch2.0 Smart") == "New Punch"

    def test_harrier_variant(self):
        assert extract_model_name("Harrier (D) XZ+") == "Harrier"

    def test_nexon_p(self):
        assert extract_model_name("Nexon-P EV Max") == "Nexon-P"

    def test_nexon_d(self):
        assert extract_model_name("Nexon-D XZ+ Diesel") == "Nexon-D"

    def test_tiago(self):
        assert extract_model_name("Tiago XE CNG") == "Tiago"

    def test_sierra(self):
        assert extract_model_name("Sierra EV Pure+") == "Sierra"

    def test_unknown_fallback(self):
        assert extract_model_name("Scorpio S11") == "Scorpio"

    def test_case_insensitive(self):
        assert extract_model_name("PUNCH 2.0 ADVENTURE") == "New Punch"


# ── Model grouping via ingestion ──────────────────────────────────────────────

class TestModelGrouping:

    def _make_excel(self, rows):
        header = ["Variant", "Ex Showroom Price", "Total (5 Years Regi)", "Total (15 Years Regi)", "Total (BH Regi)"]
        df = pd.DataFrame(rows, columns=header)
        buf = BytesIO()
        df.to_excel(buf, index=False)
        return buf.getvalue()

    def test_ingestion_populates_model_name(self, db_session, org):
        from app.models import CarModel
        content = self._make_excel([
            ["Punch2.0 Smart", 100000, 110000, 115000, 120000],
            ["Harrier XZ+", 200000, 210000, 215000, 220000],
        ])
        result = ingest_excel(content, org.id, db_session)
        assert result["rows_inserted"] == 2

        cars = db_session.query(CarModel).filter(
            CarModel.organization_id == org.id, CarModel.is_active == True
        ).order_by(CarModel.variant).all()

        assert cars[0].model_name == "Harrier"
        assert cars[1].model_name == "New Punch"


# ── Customer details validation ───────────────────────────────────────────────

class TestCustomerDetails:

    def test_full_customer_details_stored(self, client):
        resp = client.post("/create-deal", json={
            "customer_name": "Rahul Kumar",
            "phone": "9876543210",
            "variant": "pulsar ns200",
            "discount": 0,
            "address": "123 Main St, Mumbai",
            "aadhaar": "123456789012",
            "pan": "abcde1234f",
            "rse_name": "Amit Sales",
            "sm_name": "Vijay Manager",
        }, headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["address"] == "123 Main St, Mumbai"
        assert data["aadhaar"] == "123456789012"
        assert data["pan"] == "ABCDE1234F"  # uppercased
        assert data["rse_name"] == "Amit Sales"
        assert data["sm_name"] == "Vijay Manager"

    def test_non_numeric_phone_rejected(self, client):
        resp = client.post("/create-deal", json={
            "customer_name": "Test",
            "phone": "abc123",
            "variant": "pulsar ns200",
            "discount": 0,
            "address": "Addr",
            "aadhaar": "123456789012",
        }, headers=make_headers())
        assert resp.status_code == 400

    def test_no_id_doc_rejected(self, client):
        resp = client.post("/create-deal", json={
            "customer_name": "Test",
            "phone": "9876543210",
            "variant": "pulsar ns200",
            "discount": 0,
            "address": "Addr",
            # no aadhaar, pan, or voter_id
        }, headers=make_headers())
        assert resp.status_code == 400

    def test_legacy_no_details_still_works(self, client):
        resp = client.post("/create-deal", json={
            "customer_name": "Legacy User",
            "variant": "pulsar ns200",
            "discount": 0,
        }, headers=make_headers())
        assert resp.status_code == 200


# ── Full flow: pricing engine + customer details ──────────────────────────────

class TestFullDealFlow:

    def test_full_deal_with_pricing_and_customer(self, client):
        resp = client.post("/create-deal", json={
            "customer_name": "Full Flow Test",
            "phone": "9999999999",
            "variant": "pulsar ns200",
            "discount": 2000,
            "address": "456 Market Road",
            "aadhaar": "111122223333",
            "rse_name": "RSE One",
            "road_tax": 8000,
            "insurance": 6000,
        }, headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()

        # Customer details
        assert data["customer_name"] == "Full Flow Test"
        assert data["aadhaar"] == "111122223333"
        assert data["rse_name"] == "RSE One"

        # Pricing breakdown
        pb = data["pricing_breakdown"]
        assert pb is not None
        assert pb["ex_showroom"] == 150000
        assert pb["road_tax"] == 8000
        assert pb["insurance"] == 6000
        assert pb["manual_discount"] == 2000
        assert pb["final_price"] == 162000  # 150k+8k+6k - 2k

        # Approval state
        assert data["status"] in ("APPROVED", "PENDING")
        assert data["decision"] is not None
        assert data["risk_level"] is not None
