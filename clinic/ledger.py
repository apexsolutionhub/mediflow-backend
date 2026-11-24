"""Stock ledger: keeps `StockMovement` rows in step with a medicine's on-hand stock.

Call `record_movement` right after any change to `on_hand` / `loose_stock`, inside the same
transaction that locked the medicine. It writes one movement for whatever difference exists
between the ledger and the stock, so the two can never drift apart, even for edits that
did not go through a named flow.

All helpers use the database the medicine was loaded from, so they work on a clinic node's
database as well as on the cloud's.
"""
from django.db.models import Sum

from . import stock
from .models import StockMovement


def _db(medicine) -> str:
    return medicine._state.db or "default"


def ledger_total(medicine) -> int:
    total = StockMovement.objects.using(_db(medicine)).filter(medicine=medicine).aggregate(t=Sum("delta"))["t"]
    return int(total or 0)


def stock_units(medicine) -> int:
    """Stock on hand in the smallest unit it is sold in."""
    return stock.loose_capacity(medicine) if stock.is_splittable(medicine) else medicine.on_hand


def set_stock_units(medicine, units: int) -> None:
    """Put `units` (smallest unit) back into on_hand / loose_stock. Caller saves."""
    units = max(0, int(units))
    if stock.is_splittable(medicine):
        medicine.on_hand, medicine.loose_stock = divmod(units, stock.pack_size(medicine))
    else:
        medicine.on_hand, medicine.loose_stock = units, 0


def record_movement(medicine, kind=None, reference="", user=None):
    """Write the movement that brings the ledger up to the medicine's stock. Returns it, or None."""
    diff = stock_units(medicine) - ledger_total(medicine)
    if diff == 0:
        return None
    if kind is None:
        kind = StockMovement.KIND_RECEIVE if diff > 0 else StockMovement.KIND_ADJUST
    return StockMovement.objects.using(_db(medicine)).create(
        clinic_tin=medicine.clinic_tin,
        branch_name=medicine.branch_name,
        medicine=medicine,
        kind=kind,
        delta=diff,
        pack_size=stock.pack_size(medicine),
        reference=str(reference)[:64],
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )


def rebuild_stock(medicine):
    """What the stock should be, rebuilt from the ledger alone: (on_hand, loose_stock)."""
    total = max(0, ledger_total(medicine))
    if stock.is_splittable(medicine):
        return divmod(total, stock.pack_size(medicine))
    return total, 0
