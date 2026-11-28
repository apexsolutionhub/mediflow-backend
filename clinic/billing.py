"""Money rules for a visit's bill: VAT, discounts, forgiven amounts, and how a payment settles the lines.

Everything here works on one Encounter's BillableItem rows. The views call it so the cashier, the manager
approval screen and the reports all agree on the arithmetic.
"""
from decimal import ROUND_HALF_UP, Decimal

from api.audit import _person_from_request

VAT_PERCENT = Decimal("15")
CENT = Decimal("0.01")
OPEN_FORGIVENESS = ("pending", "approved", "declined")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def vat_of(amount) -> Decimal:
    """15% of a price, as shown beside each service."""
    return money(Decimal(str(amount or 0)) * VAT_PERCENT / 100)


def acting_name(request) -> str:
    """The signed-in person's name (or the login name when the role has no named person)."""
    person = _person_from_request(request)
    if person is not None:
        return person.full_name
    return getattr(request.user, "username", "") or ""


def line_due(item) -> Decimal:
    if item.payment_status == "PaymentApproved":
        return Decimal("0")
    return max(money(item.total_amount) - money(item.paid_amount), Decimal("0"))


def amount_due(encounter) -> Decimal:
    return sum((line_due(i) for i in encounter.billables.all()), Decimal("0"))


def discount_for(encounter, rule) -> Decimal:
    """What a discount rule takes off the lines it covers (only the part still unpaid)."""
    total = Decimal("0")
    for item in encounter.billables.all():
        if rule.covers(item.service_id):
            total += line_due(item) * rule.percent / 100
    return money(total)


def refundable_for(encounter, rule=None) -> Decimal:
    """What can still be given back: money paid on the covered lines, less refunds already made."""
    paid = Decimal("0")
    for item in encounter.billables.all():
        if rule is None or rule.covers(item.service_id):
            paid += money(item.paid_amount)
    already = sum((money(r.amount) for r in encounter.refunds.all()), Decimal("0"))
    return max(money(paid) - already, Decimal("0"))


def open_forgiveness(encounter):
    """The forgiven-amount request that still counts for this visit, newest first."""
    return encounter.forgiven_amounts.filter(status__in=OPEN_FORGIVENESS).first()


def settle(encounter, credit: Decimal) -> None:
    """Spread `credit` (cash + discount + forgiven) over the unpaid lines and unlock paid orders."""
    from .models import ClinicalOrder

    leftover = credit
    for item in encounter.billables.exclude(payment_status="PaymentApproved"):
        due = line_due(item)
        if due <= 0:
            item.payment_status = "PaymentApproved"
            item.save()
            continue
        applied = min(due, leftover)
        item.paid_amount = money(item.paid_amount) + applied
        leftover -= applied
        item.payment_status = "PaymentApproved" if item.paid_amount >= item.total_amount else "PartialPayment"
        item.save()
        if leftover <= 0:
            break
    for item in encounter.billables.filter(payment_status="PaymentApproved"):
        ClinicalOrder.objects.filter(billable=item, status="AwaitingPayment").update(status="PaymentApproved")
    if encounter.status == encounter.STATUS_OPEN:
        encounter.status = encounter.STATUS_ACTIVE
        encounter.save(update_fields=["status"])
