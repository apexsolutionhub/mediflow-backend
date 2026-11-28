"""
Split selling for medicines: a pack (strip, box...) can be sold whole or by the loose unit.

Stock is two numbers on the medicine: `on_hand` (whole packs) and `loose_stock` (loose units
left from an opened pack, always fewer than one pack). Selling loose first uses the opened
pack and opens a new one automatically when it runs out.
"""
from decimal import ROUND_HALF_UP, Decimal

PACK = "pack"
LOOSE = "loose"
SALE_UNITS = (PACK, LOOSE)


class StockError(Exception):
    """Raised when a sale cannot be covered by the stock on hand."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def pack_size(medicine):
    return max(1, int(medicine.pack_size or 1))


def is_splittable(medicine):
    return pack_size(medicine) > 1


def loose_label(medicine):
    return (medicine.loose_unit or "piece").lower()


def pack_label(medicine):
    return (medicine.unit_of_measure or "piece").lower()


def loose_unit_price(medicine):
    """Price of one loose unit: the manager's loose price, else pack price / pack size."""
    if medicine.loose_price is not None:
        return Decimal(medicine.loose_price)
    return (Decimal(medicine.unit_price) / pack_size(medicine)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def unit_price_for(medicine, sale_unit):
    return loose_unit_price(medicine) if sale_unit == LOOSE else Decimal(medicine.unit_price)


def loose_capacity(medicine):
    """Everything on hand, counted in loose units."""
    return medicine.on_hand * pack_size(medicine) + medicine.loose_stock


def capacity(medicine, sale_unit):
    """The most that can be sold in `sale_unit`."""
    if sale_unit == LOOSE and is_splittable(medicine):
        return loose_capacity(medicine)
    return medicine.on_hand


def reserve(medicine, sale_unit):
    """The low-stock alert level (the manager sets it in whole packs), counted in `sale_unit`."""
    packs = max(0, int(medicine.min_threshold or 0))
    return packs * pack_size(medicine) if sale_unit == LOOSE and is_splittable(medicine) else packs


def clinic_can_supply(medicine, sale_unit):
    """How much of an order the clinic pharmacy can fill without going below the low-stock alert level."""
    return max(0, capacity(medicine, sale_unit) - reserve(medicine, sale_unit))


def _plural(word, count):
    if count == 1 or word in {"ml", "dozen", "other"}:
        return word
    if word.endswith(("x", "s", "ch", "sh")):
        return f"{word}es"
    return f"{word}s"


def stock_text(medicine):
    """'3 boxes + 4 tablets', or just '3 boxes' when nothing is opened."""
    packs = f"{medicine.on_hand} {_plural(pack_label(medicine), medicine.on_hand)}"
    if is_splittable(medicine) and medicine.loose_stock:
        loose = f"{medicine.loose_stock} {_plural(loose_label(medicine), medicine.loose_stock)}"
        return loose if medicine.on_hand == 0 else f"{packs} + {loose}"
    return packs


def deduct(medicine, quantity, sale_unit):
    """Take `quantity` (in `sale_unit`) out of stock, opening packs as needed. Caller saves."""
    if quantity < 1:
        raise StockError("Quantity must be at least 1.")
    if sale_unit == LOOSE:
        if not is_splittable(medicine):
            raise StockError(f"{medicine.name} is not sold loose.")
        size = pack_size(medicine)
        if medicine.loose_stock >= quantity:
            medicine.loose_stock -= quantity
            return
        need = quantity - medicine.loose_stock
        packs_to_open = -(-need // size)
        if packs_to_open > medicine.on_hand:
            raise StockError(
                f"{medicine.name}: not enough stock. Available: {stock_text(medicine)}."
            )
        medicine.on_hand -= packs_to_open
        medicine.loose_stock = medicine.loose_stock + packs_to_open * size - quantity
        return
    if medicine.on_hand < quantity:
        raise StockError(f"{medicine.name}: not enough stock. Available: {stock_text(medicine)}.")
    medicine.on_hand -= quantity


def restock(medicine, quantity, sale_unit):
    """Put a cancelled sale back. Caller saves."""
    if sale_unit == LOOSE and is_splittable(medicine):
        extra_packs, medicine.loose_stock = divmod(medicine.loose_stock + quantity, pack_size(medicine))
        medicine.on_hand += extra_packs
        return
    medicine.on_hand += quantity


STOCK_FIELDS = ["on_hand", "loose_stock"]
