"""
Add to tenants/urls.py:

    path("renewal-status/", RenewalStatusView.as_view(), name="billing-renewal-status"),
    path("resubmit-quarterly/", ResubmitQuarterlyPaymentView.as_view(), name="billing-resubmit-quarterly"),

Deploy with the main MediFlow Django backend for quarterly renewal gating without login.
"""

from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import create_payment_submission, effective_tenant_fees
from .models import TenantAccount, TenantPaymentSubmission

User = get_user_model()


def _latest_quarterly_submission(tin: str) -> TenantPaymentSubmission | None:
    return (
        TenantPaymentSubmission.objects.filter(
            clinic_tin=tin,
            payment_kind=TenantPaymentSubmission.KIND_QUARTERLY,
        )
        .order_by("-submitted_at")
        .first()
    )


class RenewalStatusView(APIView):
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

        _setup_fee, quarterly_fee = effective_tenant_fees(tenant)

        if not tenant.setup_fee_approved:
            return Response(
                {
                    "status": "not_found",
                    "detail": "Clinic setup is not approved yet.",
                }
            )

        if tenant.subscription_payment_approved:
            return Response(
                {
                    "status": "active",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "quarterly_fee_etb": quarterly_fee,
                }
            )

        latest = _latest_quarterly_submission(tin)
        if latest and latest.status == TenantPaymentSubmission.STATUS_REJECTED:
            return Response(
                {
                    "status": "rejected",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "quarterly_fee_etb": quarterly_fee,
                    "rejection_reason": latest.rejection_reason,
                }
            )

        return Response(
            {
                "status": "pending",
                "clinic_name": tenant.clinic_name,
                "clinic_tin": tenant.clinic_tin,
                "quarterly_fee_etb": quarterly_fee,
            }
        )


class ResubmitQuarterlyPaymentView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def post(self, request):
        username = (request.data.get("username") or "").strip()
        payment_channel = (request.data.get("payment_channel") or "").strip()
        payment_transaction_ref = (request.data.get("payment_transaction_ref") or "").strip()

        if not username:
            return Response({"detail": "username is required."}, status=status.HTTP_400_BAD_REQUEST)
        if len(payment_transaction_ref) < 4:
            return Response(
                {"detail": "Transfer ID must be at least 4 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = User.objects.filter(username__iexact=username).select_related("profile").first()
        if not user or not hasattr(user, "profile"):
            return Response({"detail": "Registration not found."}, status=status.HTTP_404_NOT_FOUND)

        tin = (user.profile.clinic_tin or "").strip()
        tenant = TenantAccount.objects.filter(clinic_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        _setup_fee, quarterly_fee = effective_tenant_fees(tenant)

        if not tenant.setup_fee_approved:
            return Response(
                {"detail": "Clinic setup is not approved yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if tenant.is_illustration:
            return Response(
                {
                    "status": "exempt",
                    "is_illustration": True,
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "quarterly_fee_etb": quarterly_fee,
                    "detail": "Illustration tenant — no quarterly billing.",
                }
            )

        if tenant.subscription_payment_approved:
            return Response(
                {
                    "status": "active",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "quarterly_fee_etb": quarterly_fee,
                }
            )

        latest = _latest_quarterly_submission(tin)
        if latest and latest.status == TenantPaymentSubmission.STATUS_PENDING:
            return Response(
                {
                    "status": "pending",
                    "detail": "A quarterly payment is already awaiting Apex review.",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "quarterly_fee_etb": quarterly_fee,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        create_payment_submission(
            tenant=tenant,
            payment_kind=TenantPaymentSubmission.KIND_QUARTERLY,
            payment_channel=payment_channel,
            transaction_ref=payment_transaction_ref,
        )

        return Response(
            {
                "status": "pending",
                "clinic_name": tenant.clinic_name,
                "clinic_tin": tenant.clinic_tin,
                "quarterly_fee_etb": quarterly_fee,
                "detail": "Quarterly payment resubmitted. Awaiting Apex approval.",
            },
            status=status.HTTP_201_CREATED,
        )
