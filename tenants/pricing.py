"""MediFlow clinic pricing catalog (shared with mediflow_admin)."""

from __future__ import annotations

from .models import SubscriptionPricingRule

CLINIC_BUSINESS_TYPES = ("Clinic", "Pharmacy")
CLINIC_MODULES_KEY = ""
FALLBACK_SETUP_FEE_ETB = 15000
FALLBACK_QUARTERLY_FEE_ETB = 5000


def catalog_default_fees() -> dict:
    """
    Resolve the active clinic plan from SubscriptionPricingRule.

    mediflow_admin historically saved clinic plans as business_type=Pharmacy with
    an empty modules_key; newer entries use business_type=Clinic.
    """
    row = (
        SubscriptionPricingRule.objects.filter(
            business_type__in=CLINIC_BUSINESS_TYPES,
            modules_key=CLINIC_MODULES_KEY,
            is_active=True,
        )
        .order_by("-updated_at", "-id")
        .first()
    )
    if row:
        return {
            "setup_fee_etb": int(row.setup_fee_etb or 0),
            "quarterly_fee_etb": int(row.quarterly_fee_etb or 0),
            "source": "catalog",
            "description": (row.description or "").strip()
            or "MediFlow clinic plan — all roles, billed quarterly.",
            "pricing_rule_id": row.id,
        }

    return {
        "setup_fee_etb": FALLBACK_SETUP_FEE_ETB,
        "quarterly_fee_etb": FALLBACK_QUARTERLY_FEE_ETB,
        "source": "fallback",
        "description": "MediFlow clinic plan — all roles, billed quarterly.",
        "pricing_rule_id": None,
    }
