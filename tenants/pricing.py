"""Resolve clinic fees from the Apex pricing catalog (shared MySQL)."""

from __future__ import annotations

from .models import SubscriptionPricingRule

DEFAULT_SETUP_FEE_ETB = 15000
DEFAULT_QUARTERLY_FEE_ETB = 5000


def parse_modules(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if not isinstance(raw, list):
        return []
    return [str(m).strip() for m in raw if str(m).strip()]


def build_modules_key(modules) -> str:
    return "|".join(sorted(set(parse_modules(modules))))


def fallback_pricing() -> dict:
    return {
        "setup_fee_etb": DEFAULT_SETUP_FEE_ETB,
        "quarterly_fee_etb": DEFAULT_QUARTERLY_FEE_ETB,
        "yearly_fee_etb": 0,
        "description": "",
    }


def resolve_pricing(business_type: str = "Clinic", modules=None) -> dict:
    bt = (business_type or "Clinic").strip() or "Clinic"
    key = build_modules_key(modules or [])

    row = (
        SubscriptionPricingRule.objects.filter(
            business_type=bt,
            modules_key=key,
            is_active=True,
        )
        .order_by("sort_order", "id")
        .first()
    )

    if row is None and key == "":
        row = (
            SubscriptionPricingRule.objects.filter(
                business_type=bt,
                is_active=True,
            )
            .order_by("sort_order", "id")
            .first()
        )

    if row:
        return {
            "setup_fee_etb": int(row.setup_fee_etb or 0),
            "quarterly_fee_etb": int(row.quarterly_fee_etb or 0),
            "yearly_fee_etb": int(row.yearly_fee_etb or 0),
            "pricing_rule_id": row.id,
            "source": "catalog",
            "modules_key": row.modules_key or key,
            "description": (row.description or "").strip(),
        }

    fees = fallback_pricing()
    return {
        **fees,
        "pricing_rule_id": None,
        "source": "fallback",
        "modules_key": key,
    }


def resolve_pricing_for_tenant(tenant) -> dict:
    modules = getattr(tenant, "modules", None) or []
    return resolve_pricing("Clinic", modules)


def catalog_default_fees() -> dict:
    fees = resolve_pricing("Clinic", [])
    return {
        "setup_fee_etb": int(fees.get("setup_fee_etb") or DEFAULT_SETUP_FEE_ETB),
        "quarterly_fee_etb": int(fees.get("quarterly_fee_etb") or DEFAULT_QUARTERLY_FEE_ETB),
        "yearly_fee_etb": int(fees.get("yearly_fee_etb") or 0),
        "source": fees.get("source") or "fallback",
        "description": fees.get("description") or "",
    }
