"""
Add to tenants/urls.py:

    path("resubmit-setup/", ResubmitSetupPaymentView.as_view(), name="billing-resubmit-setup"),

Deploy with the main MediFlow Django backend so rejected signups can resubmit payment proof.
"""

from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import create_payment_submission
from .models import TenantAccount, TenantPaymentSubmission

User = get_user_model()


class ResubmitSetupPaymentView(APIView):
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

        if tenant.setup_fee_approved:
            return Response(
                {
                    "status": "approved",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": tenant.setup_fee_etb,
                }
            )

        if tenant.is_illustration:
            return Response(
                {
                    "status": "exempt",
                    "is_illustration": True,
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": tenant.setup_fee_etb,
                    "detail": "Illustration tenant — no setup resubmit required.",
                }
            )

        latest = (
            TenantPaymentSubmission.objects.filter(
                clinic_tin=tin,
                payment_kind=TenantPaymentSubmission.KIND_SETUP,
            )
            .order_by("-submitted_at")
            .first()
        )
        if latest and latest.status == TenantPaymentSubmission.STATUS_PENDING:
            return Response(
                {
                    "status": "pending",
                    "detail": "A setup payment is already awaiting Apex review.",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": tenant.setup_fee_etb,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        create_payment_submission(
            tenant=tenant,
            payment_kind=TenantPaymentSubmission.KIND_SETUP,
            payment_channel=payment_channel,
            transaction_ref=payment_transaction_ref,
        )
        tenant.payment_channel = payment_channel
        tenant.payment_transaction_ref = payment_transaction_ref
        tenant.save(update_fields=["payment_channel", "payment_transaction_ref"])

        return Response(
            {
                "status": "pending",
                "clinic_name": tenant.clinic_name,
                "clinic_tin": tenant.clinic_tin,
                "setup_fee_etb": tenant.setup_fee_etb,
                "detail": "Payment details resubmitted. Awaiting Apex approval.",
            },
            status=status.HTTP_201_CREATED,
        )
