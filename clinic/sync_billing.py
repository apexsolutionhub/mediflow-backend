"""Billing and offline clinics (docs/OFFLINE_SYNC_CONTRACT.md, section 9).

Hard lockout (trial over, grace ended...): payment proof and the approval check go to the cloud
only, so the clinic server forwards them over HTTPS and never trusts its own stale copy.

Warning window (before the hard due date): the manager's payment proof is stored on the clinic
server as usual and travels to the cloud with the next push; the cloud's verdict comes back in
the push receipt.
"""
from django.conf import settings

from tenants.billing import (
    PAYMENT_CHANNELS,
    billing_snapshot,
    create_payment_submission,
    resolve_login_access,
)
from tenants.models import TenantPaymentSubmission
from tenants.services import resolve_tenant_account

from .sync_engine import apply_tenant, export_tenant

MAX_STATUS_ROWS = 20


# ---------- cloud side ----------
def _pending_json(tin):
    pending = (
        TenantPaymentSubmission.objects.filter(clinic_tin=tin, status=TenantPaymentSubmission.STATUS_PENDING)
        .order_by("-submitted_at")
        .first()
    )
    if pending is None:
        return None
    return {
        "id": pending.id,
        "payment_kind": pending.payment_kind,
        "amount_etb": pending.amount_etb,
        "payment_channel": pending.payment_channel,
        "transaction_ref": pending.transaction_ref,
        "status": pending.status,
        "submitted_at": pending.submitted_at,
    }


def billing_me_payload(tenant, role: str) -> dict:
    """What `GET /billing/me/` answers; the cloud builds the same thing for a node in lockout."""
    access = resolve_login_access(tenant, role=role)
    return {
        "billing": billing_snapshot(tenant),
        "access_mode": access.access_mode,
        "payment_kind": access.payment_kind,
        "period_status": access.period_status,
        "channels": list(PAYMENT_CHANNELS),
        "pending_submission": _pending_json(tenant.clinic_tin),
    }


def handle_billing_status(node, body, db="default"):
    tenant = resolve_tenant_account(node.clinic_tin)
    if tenant is None:
        return 404, {"detail": "Tenant account not found."}
    return 200, {"response": billing_me_payload(tenant, "manager"), "tenant": export_tenant(node.clinic_tin)}


def handle_billing_submit(node, body, db="default"):
    tenant = resolve_tenant_account(node.clinic_tin)
    if tenant is None:
        return 404, {"detail": "Tenant account not found."}
    if tenant.is_illustration or tenant.billing_hold:
        return 400, {"detail": "Billing actions are disabled for this clinic."}
    access = resolve_login_access(tenant, role="manager")
    kind = (body.get("payment_kind") or access.payment_kind or "").strip().lower()
    if not kind:
        kind = (
            TenantPaymentSubmission.KIND_SETUP
            if not tenant.setup_fee_approved
            else TenantPaymentSubmission.KIND_QUARTERLY
        )
    try:
        submission = create_payment_submission(
            tenant=tenant,
            payment_kind=kind,
            payment_channel=body.get("payment_channel", ""),
            transaction_ref=body.get("transaction_ref", ""),
        )
    except ValueError as exc:
        return 400, {"detail": str(exc)}
    return 201, {
        "response": {
            "billing": billing_snapshot(tenant),
            "access_mode": "payment_portal",
            "payment_kind": kind,
            "detail": "Payment submitted. Apex will review shortly.",
            "submission": {
                "id": submission.id,
                "payment_kind": submission.payment_kind,
                "amount_etb": submission.amount_etb,
                "status": submission.status,
            },
        },
        "tenant": export_tenant(node.clinic_tin),
    }


def receive_submissions(tin: str, items: list) -> list:
    """Payment proofs stored on a node during the warning window: add the ones the cloud has not seen.

    Returns the cloud's current verdict on this clinic's recent submissions, for the node to mirror.
    """
    tenant = resolve_tenant_account(tin)
    if tenant is not None:
        for item in items or []:
            kind = str(item.get("payment_kind", "")).strip().lower()
            ref = str(item.get("transaction_ref", "")).strip()
            if TenantPaymentSubmission.objects.filter(clinic_tin=tin, payment_kind=kind, transaction_ref=ref).exists():
                continue
            try:
                create_payment_submission(
                    tenant=tenant,
                    payment_kind=kind,
                    payment_channel=str(item.get("payment_channel", "")),
                    transaction_ref=ref,
                )
            except ValueError:
                continue  # a malformed proof is dropped; the manager can resubmit
    rows = TenantPaymentSubmission.objects.filter(clinic_tin=tin).order_by("-submitted_at")[:MAX_STATUS_ROWS]
    return [
        {
            "payment_kind": r.payment_kind,
            "transaction_ref": r.transaction_ref,
            "status": r.status,
            "rejection_reason": r.rejection_reason,
        }
        for r in rows
    ]


# ---------- node side ----------
def proxy_enabled() -> bool:
    return (
        getattr(settings, "NODE_MODE", "cloud") == "clinic"
        and bool(settings.CLOUD_API_URL)
        and bool(settings.NODE_TOKEN)
    )


def cloud_call(path: str, body: dict) -> dict:
    from .sync_client import HttpTransport

    return HttpTransport(settings.CLOUD_API_URL, settings.NODE_TOKEN).call(path, body)


def export_local_submissions(tin: str, db="default") -> list:
    rows = TenantPaymentSubmission.objects.using(db).filter(
        clinic_tin=tin, status=TenantPaymentSubmission.STATUS_PENDING
    )
    return [
        {
            "payment_kind": r.payment_kind,
            "payment_channel": r.payment_channel,
            "transaction_ref": r.transaction_ref,
            "amount_etb": r.amount_etb,
        }
        for r in rows
    ]


def mirror_submission_status(tin: str, verdicts: list, db="default"):
    for v in verdicts or []:
        TenantPaymentSubmission.objects.using(db).filter(
            clinic_tin=tin, payment_kind=v.get("payment_kind"), transaction_ref=v.get("transaction_ref")
        ).update(status=v.get("status"), rejection_reason=v.get("rejection_reason", ""))


def adopt_cloud_tenant(tin: str, snapshot, db="default"):
    """Take the cloud's live tenant record (so an approval is seen at once, not at the next sync)."""
    apply_tenant(tin, snapshot, db=db)
