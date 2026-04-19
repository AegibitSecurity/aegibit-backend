"""
Pricing Engine — deterministic price calculation with full breakdown.

Formula (STRICT):
  final_price = ex_showroom
              + road_tax
              + insurance
              + TCS
              - total_discount
              + adjustments

TCS Rule:
  If ex_showroom >= ₹10,00,000 → TCS = 1% of ex_showroom
  Else → TCS = 0

Discount Sources:
  total_discount = corporate_discount + festival_discount + manual_discount

Validation:
  - All inputs must be non-negative
  - Final price must be > 0 (raise PricingError otherwise)
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TCS_THRESHOLD = 1_000_000       # ₹10,00,000
TCS_RATE = 0.01                 # 1%


# ─────────────────────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PricingInput:
    """Input to the pricing engine. All amounts in ₹."""
    ex_showroom: float
    road_tax: float = 0.0
    insurance: float = 0.0
    corporate_discount: float = 0.0
    festival_discount: float = 0.0
    manual_discount: float = 0.0        # maps to the user's "discount" field
    adjustments: float = 0.0            # accessories, handling charges, etc.


@dataclass(frozen=True)
class PricingBreakdown:
    """
    Immutable output of the pricing engine — full breakdown.

    Every field is a deterministic result of the formula.
    """
    ex_showroom: float
    road_tax: float
    insurance: float
    tcs: float
    subtotal: float                    # ex_showroom + road_tax + insurance + tcs
    corporate_discount: float
    festival_discount: float
    manual_discount: float
    total_discount: float
    adjustments: float
    final_price: float

    def to_dict(self) -> dict:
        """Serialize for JSON storage."""
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Errors
# ─────────────────────────────────────────────────────────────────────────────

class PricingError(Exception):
    """Raised when pricing inputs are invalid or produce an impossible price."""
    pass


# ─────────────────────────────────────────────────────────────────────────────
# Core: calculate_final_price
# ─────────────────────────────────────────────────────────────────────────────

def calculate_final_price(data: PricingInput) -> PricingBreakdown:
    """
    Compute the final price and return a full breakdown.

    This function is PURE — no DB access, no side effects, fully deterministic.

    Parameters
    ----------
    data : PricingInput
        All price components (ex-showroom, taxes, discounts, adjustments).

    Returns
    -------
    PricingBreakdown with every line item and the final price.

    Raises
    ------
    PricingError
        If any input is negative, or if the final price is ≤ 0.
    """

    # ── Validation ──────────────────────────────────────────────────────────
    _validate_non_negative(data.ex_showroom, "ex_showroom")
    _validate_non_negative(data.road_tax, "road_tax")
    _validate_non_negative(data.insurance, "insurance")
    _validate_non_negative(data.corporate_discount, "corporate_discount")
    _validate_non_negative(data.festival_discount, "festival_discount")
    _validate_non_negative(data.manual_discount, "manual_discount")
    # adjustments CAN be negative (e.g., a credit)

    # ── TCS calculation ─────────────────────────────────────────────────────
    tcs = _compute_tcs(data.ex_showroom)

    # ── Subtotal (before discounts) ─────────────────────────────────────────
    subtotal = data.ex_showroom + data.road_tax + data.insurance + tcs

    # ── Total discount ──────────────────────────────────────────────────────
    total_discount = (
        data.corporate_discount
        + data.festival_discount
        + data.manual_discount
    )

    # ── Final price ─────────────────────────────────────────────────────────
    final_price = subtotal - total_discount + data.adjustments

    # ── Post-validation ─────────────────────────────────────────────────────
    if final_price <= 0:
        raise PricingError(
            f"Final price is ₹{final_price:,.0f} (must be > 0). "
            f"Subtotal ₹{subtotal:,.0f} - discount ₹{total_discount:,.0f} "
            f"+ adjustments ₹{data.adjustments:,.0f}."
        )

    return PricingBreakdown(
        ex_showroom=data.ex_showroom,
        road_tax=data.road_tax,
        insurance=data.insurance,
        tcs=tcs,
        subtotal=subtotal,
        corporate_discount=data.corporate_discount,
        festival_discount=data.festival_discount,
        manual_discount=data.manual_discount,
        total_discount=total_discount,
        adjustments=data.adjustments,
        final_price=final_price,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pure helpers
# ─────────────────────────────────────────────────────────────────────────────

def _compute_tcs(ex_showroom: float) -> float:
    """
    TCS (Tax Collected at Source):
      ≥ ₹10,00,000 → 1% of ex_showroom
      < ₹10,00,000 → 0
    """
    if ex_showroom >= TCS_THRESHOLD:
        return round(ex_showroom * TCS_RATE, 2)
    return 0.0


def _validate_non_negative(value: float, field_name: str):
    """Raise PricingError if value is negative."""
    if value < 0:
        raise PricingError(
            f"{field_name} cannot be negative (got ₹{value:,.0f})."
        )
