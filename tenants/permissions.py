from rest_framework.permissions import BasePermission

from .billing import resolve_login_access
from .models import TenantAccount
from .services import resolve_tenant_account


class TenantBillingAccessPermission(BasePermission):
    message = "Subscription payment is required. Ask your clinic manager to renew."

    def has_permission(self, request, view):
        profile = getattr(request.user, "profile", None)
        if not profile:
            return False
        tenant = resolve_tenant_account((profile.clinic_tin or "").strip())
        if not tenant:
            return False
        decision = resolve_login_access(tenant, role=profile.role)
        if decision.access_mode != "full":
            return False
        # An offline clinic server that is overdue for a sync is closed to everyone but Sync and Billing.
        from clinic.sync_gate import STAFF_MESSAGE, gate_for_profile

        if gate_for_profile(profile, tenant).get("overdue"):
            self.message = STAFF_MESSAGE
            return False
        return True
