"""
Tests for deal creation and approval flow via the API.

Covers:
  - Deal creation pipeline (pricing lookup → Profit Guard → persist)
  - Auto-approve flow
  - GM approval flow
  - Director approval flow
  - Escalation from GM → Director
  - Deal rejection
  - Role-based access control (RBAC)
  - Org isolation

All tests use the FastAPI TestClient with in-memory SQLite.
"""

import pytest

from tests.conftest import make_headers


# ─────────────────────────────────────────────────────────────────────────────
# Deal Creation
# ─────────────────────────────────────────────────────────────────────────────

class TestDealCreation:

    def test_create_deal_auto_approved(self, client):
        """Deal with no discount → AUTO_APPROVE."""
        resp = client.post("/create-deal", json={
            "customer_name": "John Doe",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": 0,
        }, headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["decision"] == "AUTO_APPROVE"
        assert data["approval_stage"] == "DONE"
        assert data["customer_name"] == "John Doe"

    def test_create_deal_gm_approval(self, client):
        """Discount exceeding GM limit → PENDING, GM stage."""
        resp = client.post("/create-deal", json={
            "customer_name": "Jane Smith",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": 7000,
        }, headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "PENDING"
        assert data["decision"] == "GM_APPROVAL"
        assert data["approval_stage"] == "GM"

    def test_create_deal_director_approval(self, client):
        """Loss-level discount → PENDING, DIRECTOR stage."""
        resp = client.post("/create-deal", json={
            "customer_name": "Big Buyer",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": 25000,  # final=145000 < cost=150000 → loss → DIRECTOR
        }, headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "PENDING"
        assert data["decision"] == "DIRECTOR_APPROVAL"
        assert data["approval_stage"] == "DIRECTOR"

    def test_create_deal_unknown_variant_404(self, client):
        """Unknown variant → 404."""
        resp = client.post("/create-deal", json={
            "customer_name": "Test",
            "variant": "nonexistent-model-xyz",
            "registration_type": "5YR",
            "discount": 0,
        }, headers=make_headers())
        assert resp.status_code == 404

    def test_create_deal_missing_customer_400(self, client):
        """Empty customer name → 400."""
        resp = client.post("/create-deal", json={
            "customer_name": "",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": 0,
        }, headers=make_headers())
        assert resp.status_code == 400

    def test_create_deal_negative_discount_400(self, client):
        """Negative discount → 400."""
        resp = client.post("/create-deal", json={
            "customer_name": "Test",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": -100,
        }, headers=make_headers())
        assert resp.status_code == 400

    def test_deal_appears_in_list(self, client):
        """Created deal shows up in GET /deals."""
        client.post("/create-deal", json={
            "customer_name": "List Test",
            "variant": "pulsar ns200",
            "discount": 0,
        }, headers=make_headers())

        resp = client.get("/deals", headers=make_headers())
        assert resp.status_code == 200
        deals = resp.json()
        assert any(d["customer_name"] == "List Test" for d in deals)


# ─────────────────────────────────────────────────────────────────────────────
# Approval Flow
# ─────────────────────────────────────────────────────────────────────────────

class TestApprovalFlow:

    def _create_pending_deal(self, client, discount=7000):
        """Helper: create a deal that needs GM approval."""
        resp = client.post("/create-deal", json={
            "customer_name": "Approval Test",
            "variant": "pulsar ns200",
            "registration_type": "5YR",
            "discount": discount,
        }, headers=make_headers())
        return resp.json()

    def test_gm_approve(self, client):
        """GM approves → deal becomes APPROVED."""
        deal = self._create_pending_deal(client, discount=7000)
        assert deal["status"] == "PENDING"

        resp = client.post("/approve-gm", json={
            "deal_id": deal["id"],
        }, headers=make_headers(role="GM"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "APPROVED"
        assert data["approval_stage"] == "DONE"

    def test_director_approve(self, client):
        """Director approves deal needing director approval."""
        deal = self._create_pending_deal(client, discount=25000)  # loss → DIRECTOR
        assert deal["approval_stage"] == "DIRECTOR"

        resp = client.post("/approve-director", json={
            "deal_id": deal["id"],
        }, headers=make_headers(role="DIRECTOR"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "APPROVED"

    def test_reject_deal(self, client):
        """GM rejects a pending deal."""
        deal = self._create_pending_deal(client)

        resp = client.post("/reject", json={
            "deal_id": deal["id"],
        }, headers=make_headers(role="GM"))
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "REJECTED"
        assert data["approval_stage"] == "DONE"

    def test_cannot_approve_already_approved(self, client):
        """Approving an already-approved deal → 400."""
        deal = self._create_pending_deal(client)
        # First approve
        client.post("/approve-gm", json={"deal_id": deal["id"]},
                     headers=make_headers(role="GM"))
        # Second approve should fail
        resp = client.post("/approve-gm", json={"deal_id": deal["id"]},
                           headers=make_headers(role="GM"))
        assert resp.status_code == 400

    def test_cannot_reject_already_rejected(self, client):
        """Rejecting an already-rejected deal → 400."""
        deal = self._create_pending_deal(client)
        client.post("/reject", json={"deal_id": deal["id"]},
                     headers=make_headers(role="GM"))
        resp = client.post("/reject", json={"deal_id": deal["id"]},
                           headers=make_headers(role="GM"))
        assert resp.status_code == 400


# ─────────────────────────────────────────────────────────────────────────────
# RBAC
# ─────────────────────────────────────────────────────────────────────────────

class TestRoleBasedAccess:

    def test_sales_cannot_approve(self, client):
        """SALES role → 403 on /approve-gm."""
        # Create a deal first (as admin)
        deal_resp = client.post("/create-deal", json={
            "customer_name": "RBAC Test",
            "variant": "pulsar ns200",
            "discount": 7000,
        }, headers=make_headers(role="ADMIN"))
        deal = deal_resp.json()

        # Try to approve as SALES
        resp = client.post("/approve-gm", json={"deal_id": deal["id"]},
                           headers=make_headers(role="SALES"))
        assert resp.status_code == 403

    def test_gm_cannot_director_approve(self, client):
        """GM role → 403 on /approve-director."""
        deal_resp = client.post("/create-deal", json={
            "customer_name": "RBAC Test 2",
            "variant": "pulsar ns200",
            "discount": 18000,
        }, headers=make_headers(role="ADMIN"))
        deal = deal_resp.json()

        resp = client.post("/approve-director", json={"deal_id": deal["id"]},
                           headers=make_headers(role="GM"))
        assert resp.status_code == 403

    def test_admin_can_do_everything(self, client):
        """ADMIN can create, approve, etc."""
        resp = client.post("/create-deal", json={
            "customer_name": "Admin Deal",
            "variant": "pulsar ns200",
            "discount": 7000,
        }, headers=make_headers(role="ADMIN"))
        assert resp.status_code == 200
        deal = resp.json()

        resp = client.post("/approve-gm", json={"deal_id": deal["id"]},
                           headers=make_headers(role="ADMIN"))
        assert resp.status_code == 200

    def test_sales_can_create_deal(self, client):
        """SALES role can create deals."""
        resp = client.post("/create-deal", json={
            "customer_name": "Sales Deal",
            "variant": "pulsar ns200",
            "discount": 0,
        }, headers=make_headers(role="SALES"))
        assert resp.status_code == 200

    def test_missing_org_header_401(self, client):
        """No x-org-id header → 422 (FastAPI header validation)."""
        resp = client.get("/deals", headers={"X-Role": "ADMIN"})
        assert resp.status_code == 422

    def test_invalid_role_400(self, client):
        """Invalid role string → 400."""
        resp = client.get("/deals", headers={
            "X-Org-Id": "test-org-001",
            "X-Role": "HACKER",
        })
        assert resp.status_code == 400

    def test_nonexistent_org_403(self, client):
        """Valid headers but org doesn't exist → 403."""
        resp = client.get("/deals", headers={
            "X-Org-Id": "fake-org-id",
            "X-Role": "ADMIN",
        })
        assert resp.status_code == 403


# ─────────────────────────────────────────────────────────────────────────────
# Deal Detail & Audit Trail
# ─────────────────────────────────────────────────────────────────────────────

class TestDealDetail:

    def test_deal_detail_has_events(self, client):
        """GET /deals/{id} returns audit events."""
        resp = client.post("/create-deal", json={
            "customer_name": "Detail Test",
            "variant": "pulsar ns200",
            "discount": 0,
        }, headers=make_headers())
        deal = resp.json()

        resp = client.get(f"/deals/{deal['id']}", headers=make_headers())
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert len(data["events"]) >= 1
        assert data["events"][0]["event_type"] == "DEAL_CREATED"

    def test_deal_detail_nonexistent_404(self, client):
        resp = client.get("/deals/nonexistent-id", headers=make_headers())
        assert resp.status_code == 404
