from rest_framework.permissions import BasePermission

from .billing import resolve_login_access
from .models import TenantAccount


class TenantBillingAccessPermission(BasePermission):
    message = "Subscription payment is required. Ask your clinic manager to renew."

    def has_permission(self, request, view):
        profile = getattr(request.user, "profile", None)
        if not profile:
            return False
        tenant = TenantAccount.objects.filter(clinic_tin=(profile.clinic_tin or "").strip()).first()
        if not tenant:
            return False
        decision = resolve_login_access(tenant, role=profile.role)
        return decision.access_mode == "full"
