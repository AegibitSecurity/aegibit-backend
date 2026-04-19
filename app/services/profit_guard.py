"""
Profit Guard Engine — the core decision logic for deal approvals.

Takes a deal's base_price, discount, and cost (ex-showroom as proxy)
and produces: margin, margin %, deal health, and a routing recommendation.

Decision Hierarchy
------------------
1. Negative margin (loss deal)        → DIRECTOR_APPROVAL  + LOSS
2. Margin below org threshold         → GM_APPROVAL        + RISKY
3. Discount exceeds director limit    → DIRECTOR_APPROVAL  + RISKY
4. Discount exceeds GM limit          → GM_APPROVAL        + RISKY
5. Everything within limits           → AUTO_APPROVE        + GOOD
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import OrgConfig


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProfitGuardResult:
    """Immutable result of a Profit Guard evaluation."""

    margin: float               # final_price - cost  (absolute ₹)
    margin_percent: float       # margin / cost * 100
    risk_level: str             # LOW | MEDIUM | HIGH
    deal_health: str            # GOOD | RISKY | LOSS
    decision: str               # AUTO_APPROVE | GM_APPROVAL | DIRECTOR_APPROVAL
    reason: str                 # Human-readable explanation
    urgency: str = "NORMAL"     # NORMAL | HIGH | CRITICAL


# ── Constants ────────────────────────────────────────────────────────────────

AUTO_APPROVE      = "AUTO_APPROVE"
GM_APPROVAL       = "GM_APPROVAL"
DIRECTOR_APPROVAL = "DIRECTOR_APPROVAL"

GOOD  = "GOOD"
RISKY = "RISKY"
LOSS  = "LOSS"

LOW      = "LOW"
MEDIUM   = "MEDIUM"
HIGH     = "HIGH"

NORMAL   = "NORMAL"
_HIGH    = "HIGH"       # prefixed to avoid shadowing risk_level HIGH
CRITICAL = "CRITICAL"


# ── Core evaluation ─────────────────────────────────────────────────────────

def evaluate(
    base_price: float,
    discount: float,
    cost: float,
    org_config: OrgConfig,
) -> ProfitGuardResult:
    """
    Run the Profit Guard decision engine on a single deal.

    Parameters
    ----------
    base_price  : Registration-type-specific total price (5yr / 15yr / BH)
    discount    : Flat discount amount offered to the customer
    cost        : Cost price proxy — typically ex-showroom price
    org_config  : The organisation's configurable approval thresholds:
                  - gm_discount_limit       (₹ amount)
                  - director_discount_limit  (₹ amount)
                  - min_margin_threshold     (% of cost)

    Returns
    -------
    ProfitGuardResult with margin, deal health, decision, and reason.
    """

    # ── 1. Calculate margin ──────────────────────────────────────────────────
    final_price = base_price - discount
    margin = _compute_margin(final_price, cost)
    margin_percent = _compute_margin_percent(margin, cost)

    # ── 2. Decision tree (evaluated top → bottom, first match wins) ──────────
    #
    #   Priority:
    #     ① Loss deal         (margin < 0)                → DIRECTOR + LOSS
    #     ② Below margin %    (margin% < min_threshold)   → GM       + RISKY
    #     ③ Director discount (discount > director_limit) → DIRECTOR + RISKY
    #     ④ GM discount       (discount > gm_limit)       → GM       + RISKY
    #     ⑤ Healthy deal                                  → AUTO     + GOOD

    # ── Rule 1: Loss deal — negative margin ──────────────────────────────────
    if margin < 0:
        return ProfitGuardResult(
            margin=margin,
            margin_percent=margin_percent,
            risk_level=HIGH,
            deal_health=LOSS,
            decision=DIRECTOR_APPROVAL,
            reason=(
                f"Loss deal: margin is ₹{margin:,.0f} "
                f"({margin_percent:+.1f}%). "
                f"Final price ₹{final_price:,.0f} is below cost ₹{cost:,.0f}."
            ),
            urgency=CRITICAL,
        )

    # ── Rule 2: Margin below org threshold ───────────────────────────────────
    if margin_percent < org_config.min_margin_threshold:
        return ProfitGuardResult(
            margin=margin,
            margin_percent=margin_percent,
            risk_level=MEDIUM,
            deal_health=RISKY,
            decision=GM_APPROVAL,
            reason=(
                f"Low margin: {margin_percent:.1f}% is below the "
                f"{org_config.min_margin_threshold}% threshold."
            ),
            urgency=_HIGH,
        )

    # ── Rule 3: Discount exceeds director limit ──────────────────────────────
    if discount > org_config.director_discount_limit:
        return ProfitGuardResult(
            margin=margin,
            margin_percent=margin_percent,
            risk_level=HIGH,
            deal_health=RISKY,
            decision=DIRECTOR_APPROVAL,
            reason=(
                f"Discount ₹{discount:,.0f} exceeds director limit "
                f"₹{org_config.director_discount_limit:,.0f}."
            ),
            urgency=CRITICAL,
        )

    # ── Rule 4: Discount exceeds GM limit ────────────────────────────────────
    if discount > org_config.gm_discount_limit:
        return ProfitGuardResult(
            margin=margin,
            margin_percent=margin_percent,
            risk_level=MEDIUM,
            deal_health=RISKY,
            decision=GM_APPROVAL,
            reason=(
                f"Discount ₹{discount:,.0f} exceeds GM limit "
                f"₹{org_config.gm_discount_limit:,.0f}."
            ),
            urgency=_HIGH,
        )

    # ── Rule 5: Healthy deal — within all limits ─────────────────────────────
    return ProfitGuardResult(
        margin=margin,
        margin_percent=margin_percent,
        risk_level=LOW,
        deal_health=GOOD,
        decision=AUTO_APPROVE,
        reason=_build_healthy_reason(margin, margin_percent, discount),
        urgency=NORMAL,
    )


# ── Pure helper functions ────────────────────────────────────────────────────

def _compute_margin(final_price: float, cost: float) -> float:
    """Margin = what the dealership keeps above cost."""
    return final_price - cost


def _compute_margin_percent(margin: float, cost: float) -> float:
    """Margin as a percentage of cost.  Returns 0 if cost is zero."""
    if cost == 0:
        return 0.0
    return (margin / cost) * 100


def _build_healthy_reason(margin: float, margin_pct: float, discount: float) -> str:
    """Compose a human-readable reason for an auto-approved deal."""
    parts: list[str] = []
    if discount == 0:
        parts.append("No discount applied")
    else:
        parts.append(f"Discount ₹{discount:,.0f} within limits")
    parts.append(f"margin ₹{margin:,.0f} ({margin_pct:.1f}%)")
    return " — ".join(parts) + " — auto-approved."
