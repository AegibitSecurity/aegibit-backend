"""
Tests for the Profit Guard decision engine.

Covers all 5 rules in the decision tree:
  1. Loss deal (negative margin)       → DIRECTOR_APPROVAL
  2. Low margin (below threshold)      → GM_APPROVAL
  3. Discount > director limit         → DIRECTOR_APPROVAL
  4. Discount > GM limit               → GM_APPROVAL
  5. Healthy deal                      → AUTO_APPROVE
"""

import pytest
from app.services.profit_guard import (
    evaluate,
    ProfitGuardResult,
    AUTO_APPROVE,
    GM_APPROVAL,
    DIRECTOR_APPROVAL,
)


class FakeOrgConfig:
    """Minimal OrgConfig stand-in for unit tests."""
    def __init__(
        self,
        gm_discount_limit=5000,
        director_discount_limit=15000,
        min_margin_threshold=3.0,
    ):
        self.gm_discount_limit = gm_discount_limit
        self.director_discount_limit = director_discount_limit
        self.min_margin_threshold = min_margin_threshold


# ─────────────────────────────────────────────────────────────────────────────
# Rule 5: Healthy deal → AUTO_APPROVE
# ─────────────────────────────────────────────────────────────────────────────

class TestAutoApprove:
    """Deals within all limits should auto-approve."""

    def test_no_discount(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=170000, discount=0, cost=150000, org_config=cfg)
        assert result.decision == AUTO_APPROVE
        assert result.deal_health == "GOOD"
        assert result.risk_level == "LOW"
        assert result.margin == 20000  # 170000 - 150000
        assert result.margin_percent == pytest.approx(13.33, abs=0.1)

    def test_small_discount_within_gm_limit(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=170000, discount=3000, cost=150000, org_config=cfg)
        assert result.decision == AUTO_APPROVE
        assert result.margin == 17000  # (170000-3000) - 150000

    def test_zero_discount_reason_contains_no_discount(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=170000, discount=0, cost=150000, org_config=cfg)
        assert "No discount applied" in result.reason
        assert "auto-approved" in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4: Discount > GM limit → GM_APPROVAL
# ─────────────────────────────────────────────────────────────────────────────

class TestGMApproval:
    """Discounts exceeding GM limit but below director limit → GM_APPROVAL."""

    def test_discount_exceeds_gm_limit(self):
        cfg = FakeOrgConfig(gm_discount_limit=5000, director_discount_limit=15000)
        result = evaluate(base_price=170000, discount=7000, cost=150000, org_config=cfg)
        assert result.decision == GM_APPROVAL
        assert result.deal_health == "RISKY"
        assert result.risk_level == "MEDIUM"

    def test_discount_exactly_at_gm_limit_auto_approves(self):
        cfg = FakeOrgConfig(gm_discount_limit=5000)
        result = evaluate(base_price=170000, discount=5000, cost=150000, org_config=cfg)
        # Exactly at limit should auto-approve (only > triggers)
        assert result.decision == AUTO_APPROVE

    def test_discount_just_over_gm_limit(self):
        cfg = FakeOrgConfig(gm_discount_limit=5000)
        result = evaluate(base_price=170000, discount=5001, cost=150000, org_config=cfg)
        assert result.decision == GM_APPROVAL

    def test_reason_mentions_gm_limit(self):
        cfg = FakeOrgConfig(gm_discount_limit=5000)
        result = evaluate(base_price=170000, discount=8000, cost=150000, org_config=cfg)
        assert "GM limit" in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Rule 3: Discount > director limit → DIRECTOR_APPROVAL
# ─────────────────────────────────────────────────────────────────────────────

class TestDirectorApproval:
    """Discounts exceeding director limit → DIRECTOR_APPROVAL."""

    def test_discount_exceeds_director_limit(self):
        cfg = FakeOrgConfig(director_discount_limit=15000, min_margin_threshold=1.0)
        # base=200000, disc=20000, cost=150000 → final=180000, margin=30000 (20%)
        # margin is healthy, but discount 20000 > director_limit 15000
        result = evaluate(base_price=200000, discount=20000, cost=150000, org_config=cfg)
        assert result.decision == DIRECTOR_APPROVAL
        assert result.risk_level == "HIGH"

    def test_discount_exactly_at_director_limit_goes_to_gm(self):
        cfg = FakeOrgConfig(gm_discount_limit=5000, director_discount_limit=15000, min_margin_threshold=1.0)
        # base=200000, disc=15000, cost=150000 → final=185000, margin=35000 (23.3%)
        # 15000 is NOT > 15000, but IS > 5000 → GM
        result = evaluate(base_price=200000, discount=15000, cost=150000, org_config=cfg)
        assert result.decision == GM_APPROVAL

    def test_reason_mentions_director_limit(self):
        cfg = FakeOrgConfig(director_discount_limit=15000, min_margin_threshold=1.0)
        result = evaluate(base_price=200000, discount=20000, cost=150000, org_config=cfg)
        assert "director limit" in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2: Low margin → GM_APPROVAL
# ─────────────────────────────────────────────────────────────────────────────

class TestLowMargin:
    """Margin below threshold → GM_APPROVAL (even if discount is within limits)."""

    def test_low_margin_triggers_gm(self):
        # margin = (170000 - 4000) - 166500 = -500 → actually that's loss
        # Let's set cost high so margin % is low but positive
        cfg = FakeOrgConfig(min_margin_threshold=3.0, gm_discount_limit=5000)
        # base=170000, discount=3000, cost=165000 → margin=2000, pct=1.2% < 3%
        result = evaluate(base_price=170000, discount=3000, cost=165000, org_config=cfg)
        assert result.decision == GM_APPROVAL
        assert result.deal_health == "RISKY"
        assert result.margin_percent < 3.0

    def test_margin_exactly_at_threshold_auto_approves(self):
        cfg = FakeOrgConfig(min_margin_threshold=3.0)
        # Need margin% == 3.0 exactly: margin/cost*100 = 3 → margin = cost*0.03
        # cost=100000, margin=3000, final=103000, base-discount=103000
        result = evaluate(base_price=103000, discount=0, cost=100000, org_config=cfg)
        assert result.decision == AUTO_APPROVE  # 3.0 is not < 3.0


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1: Loss deal → DIRECTOR_APPROVAL
# ─────────────────────────────────────────────────────────────────────────────

class TestLossDeal:
    """Negative margin → immediate DIRECTOR_APPROVAL with LOSS health."""

    def test_loss_deal(self):
        cfg = FakeOrgConfig()
        # cost=150000, final=140000 → margin=-10000
        result = evaluate(base_price=170000, discount=30000, cost=150000, org_config=cfg)
        assert result.decision == DIRECTOR_APPROVAL
        assert result.deal_health == "LOSS"
        assert result.risk_level == "HIGH"
        assert result.margin < 0
        assert result.urgency == "CRITICAL"

    def test_loss_deal_reason_mentions_loss(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=170000, discount=30000, cost=150000, org_config=cfg)
        assert "Loss deal" in result.reason
        assert "below cost" in result.reason


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────

class TestEdgeCases:
    """Boundary and unusual scenarios."""

    def test_zero_cost_no_division_error(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=10000, discount=0, cost=0, org_config=cfg)
        assert result.margin_percent == 0.0
        # margin_percent=0.0 < min_margin_threshold=3.0 → GM_APPROVAL
        assert result.decision == GM_APPROVAL

    def test_result_is_frozen_dataclass(self):
        cfg = FakeOrgConfig()
        result = evaluate(base_price=170000, discount=0, cost=150000, org_config=cfg)
        assert isinstance(result, ProfitGuardResult)
        with pytest.raises(AttributeError):
            result.margin = 999

    def test_different_org_thresholds(self):
        # Org with very tight limits
        tight = FakeOrgConfig(gm_discount_limit=1000, director_discount_limit=3000, min_margin_threshold=10.0)
        result = evaluate(base_price=170000, discount=2000, cost=150000, org_config=tight)
        assert result.decision == GM_APPROVAL  # 2000 > 1000

        # Same deal with loose limits
        loose = FakeOrgConfig(gm_discount_limit=50000, director_discount_limit=100000, min_margin_threshold=1.0)
        result = evaluate(base_price=170000, discount=2000, cost=150000, org_config=loose)
        assert result.decision == AUTO_APPROVE
