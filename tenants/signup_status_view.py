"""
Signup registration status (public).

Add to tenants/urls.py (replace import from .views if needed):

    from .signup_status_view import SignupRegistrationStatusView
"""

from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import effective_tenant_fees
from .models import TenantAccount, TenantPaymentSubmission

User = get_user_model()


class SignupRegistrationStatusView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        username = (request.query_params.get("username") or "").strip()
        if not username:
            return Response({"detail": "username is required."}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(username__iexact=username).select_related("profile").first()
        if not user or not hasattr(user, "profile"):
            return Response({"status": "not_found", "detail": "Registration not found."})

        tin = (user.profile.clinic_tin or "").strip()
        tenant = TenantAccount.objects.filter(clinic_tin=tin).first()
        if not tenant:
            return Response({"status": "not_found", "detail": "Tenant not found."})

        setup_fee, _quarterly_fee = effective_tenant_fees(tenant)

        if tenant.is_illustration:
            return Response(
                {
                    "status": "exempt",
                    "is_illustration": True,
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "detail": "Illustration tenant — sign in directly for demos.",
                }
            )

        if getattr(tenant, "provisioned_by_apex", False):
            return Response(
                {
                    "status": "approved",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": setup_fee,
                    "provisioned_by_apex": True,
                }
            )

        if tenant.setup_fee_approved or setup_fee <= 0:
            return Response(
                {
                    "status": "approved",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": setup_fee,
                }
            )

        pending = (
            TenantPaymentSubmission.objects.filter(
                clinic_tin=tin,
                payment_kind=TenantPaymentSubmission.KIND_SETUP,
            )
            .order_by("-submitted_at")
            .first()
        )
        if pending and pending.status == TenantPaymentSubmission.STATUS_REJECTED:
            return Response(
                {
                    "status": "rejected",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": setup_fee,
                    "rejection_reason": pending.rejection_reason,
                }
            )
        return Response(
            {
                "status": "pending",
                "clinic_name": tenant.clinic_name,
                "clinic_tin": tenant.clinic_tin,
                "setup_fee_etb": setup_fee,
            }
        )
