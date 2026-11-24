"""Sync freshness gate (docs/OFFLINE_SYNC_CONTRACT.md, section 8.4).

An offline clinic's server may go only so long without a successful sync:

  more than 7 days from the due date (trial end or subscription end)  ->  7 calendar days
  7 days or fewer from the due date                                    ->  2 calendar days

Past that, staff cannot sign in or call the clinic API. The manager can still open Sync and
Billing to fix it. The gate only exists on a clinic server for a tenant in offline mode; the
cloud and online tenants are never gated.
"""
from django.conf import settings
from django.utils import timezone

from tenants.billing import compute_period_status, days_until_due
from tenants.models import TenantAccount

from .models import SyncState

FAR_DAYS = 7
NEAR_DAYS = 2
NEAR_DUE_WITHIN = 7

STAFF_MESSAGE = "This clinic server has not synced with the cloud recently. Ask your manager to run a sync before you sign in."


def gate_applies(tenant) -> bool:
    return (
        getattr(settings, "NODE_MODE", "cloud") == "clinic"
        and (getattr(tenant, "ops_mode", "") or "").strip().lower() == TenantAccount.OPS_MODE_OFFLINE
    )


def allowed_days(tenant, now=None) -> int:
    due_in = days_until_due(tenant, compute_period_status(tenant, now=now), now=now)
    return NEAR_DAYS if due_in is not None and due_in <= NEAR_DUE_WITHIN else FAR_DAYS


def sync_gate(tenant, branch_name: str, now=None) -> dict:
    """Where this branch stands against the freshness rule. Safe to call for any tenant."""
    if tenant is None or not gate_applies(tenant):
        return {"applies": False, "overdue": False, "reason": None}
    now = now or timezone.now()
    max_days = allowed_days(tenant, now)
    branch = (branch_name or "").strip() or "Main"
    state = SyncState.objects.filter(clinic_tin=tenant.clinic_tin, branch_name__iexact=branch).first()
    last = state.last_successful_sync_at if state else None
    gate = {
        "applies": True,
        "overdue": False,
        "reason": None,
        "max_days": max_days,
        "last_successful_sync_at": last,
        "days_since_sync": None,
    }
    if state is not None and state.restore_pending:
        gate.update(overdue=True, reason="restored")
    elif last is None:
        gate.update(overdue=True, reason="never_synced")
    else:
        days = (now.date() - last.date()).days
        gate["days_since_sync"] = days
        if days > max_days:
            gate.update(overdue=True, reason="stale")
    return gate


def gate_for_profile(profile, tenant=None):
    from tenants.services import resolve_tenant_account

    if profile is None:
        return {"applies": False, "overdue": False, "reason": None}
    tenant = tenant or resolve_tenant_account((profile.clinic_tin or "").strip())
    return sync_gate(tenant, profile.branch_name)


def apply_gate(decision, gate: dict, role: str):
    """Fold the sync gate into a login decision: staff are denied, the manager is limited to Sync + Billing."""
    if not gate.get("overdue") or decision.access_mode != "full":
        return decision
    if (role or "").strip().lower() == "manager":
        decision.access_mode = "sync_required"
        decision.detail = "Sync is overdue. Run a sync to unlock the clinic."
    else:
        decision.access_mode = "denied"
        decision.detail = STAFF_MESSAGE
    return decision
