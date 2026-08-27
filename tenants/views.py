from django.contrib.auth import get_user_model
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .billing import (
    PAYMENT_CHANNELS,
    billing_snapshot,
    catalog_default_fees,
    create_payment_submission,
    resolve_login_access,
)
from .models import TenantAccount, TenantPaymentSubmission

User = get_user_model()


class PublicPricingView(APIView):
    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request):
        fees = catalog_default_fees()
        return Response(
            {
                "business_type": "Clinic",
                "product": "clinic",
                "setup_fee_etb": fees["setup_fee_etb"],
                "quarterly_fee_etb": fees["quarterly_fee_etb"],
                "channels": list(PAYMENT_CHANNELS),
                "source": fees.get("source") or "catalog",
                "description": fees.get("description") or "",
            }
        )


class SubmitTenantPaymentView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile or (profile.role or "").strip().lower() != "manager":
            return Response(
                {"detail": "Only clinic managers can submit payments."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tin = (profile.clinic_tin or "").strip()
        tenant = TenantAccount.objects.filter(clinic_tin=tin).first()
        if not tenant:
            return Response({"detail": "Tenant account not found."}, status=status.HTTP_404_NOT_FOUND)

        if tenant.is_illustration or tenant.billing_hold:
            return Response(
                {"detail": "Billing actions are disabled for this clinic."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        access = resolve_login_access(tenant, role=profile.role)
        payment_kind = (request.data.get("payment_kind") or access.payment_kind or "").strip().lower()
        if not payment_kind:
            if not tenant.setup_fee_approved:
                payment_kind = TenantPaymentSubmission.KIND_SETUP
            else:
                payment_kind = TenantPaymentSubmission.KIND_QUARTERLY

        try:
            submission = create_payment_submission(
                tenant=tenant,
                payment_kind=payment_kind,
                payment_channel=request.data.get("payment_channel", ""),
                transaction_ref=request.data.get("transaction_ref", ""),
                submitted_by=request.user,
            )
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response(
            {
                "billing": billing_snapshot(tenant),
                "access_mode": "payment_portal",
                "payment_kind": payment_kind,
                "detail": "Payment submitted. Apex will review shortly.",
                "submission": {
                    "id": submission.id,
                    "payment_kind": submission.payment_kind,
                    "amount_etb": submission.amount_etb,
                    "status": submission.status,
                },
            },
            status=status.HTTP_201_CREATED,
        )


class BillingMeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile:
            return Response({"detail": "Profile not found."}, status=status.HTTP_404_NOT_FOUND)
        tenant = TenantAccount.objects.filter(clinic_tin=profile.clinic_tin).first()
        if not tenant:
            return Response({"detail": "Tenant account not found."}, status=status.HTTP_404_NOT_FOUND)

        access = resolve_login_access(tenant, role=profile.role)
        pending = (
            TenantPaymentSubmission.objects.filter(
                clinic_tin=tenant.clinic_tin,
                status=TenantPaymentSubmission.STATUS_PENDING,
            )
            .order_by("-submitted_at")
            .first()
        )
        return Response(
            {
                "billing": billing_snapshot(tenant),
                "access_mode": access.access_mode,
                "payment_kind": access.payment_kind,
                "period_status": access.period_status,
                "channels": list(PAYMENT_CHANNELS),
                "pending_submission": None
                if not pending
                else {
                    "id": pending.id,
                    "payment_kind": pending.payment_kind,
                    "amount_etb": pending.amount_etb,
                    "payment_channel": pending.payment_channel,
                    "transaction_ref": pending.transaction_ref,
                    "status": pending.status,
                    "submitted_at": pending.submitted_at,
                },
            }
        )


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

        if tenant.setup_fee_approved or int(tenant.setup_fee_etb or 0) <= 0:
            return Response(
                {
                    "status": "approved",
                    "clinic_name": tenant.clinic_name,
                    "clinic_tin": tenant.clinic_tin,
                    "setup_fee_etb": tenant.setup_fee_etb,
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
                    "setup_fee_etb": tenant.setup_fee_etb,
                    "rejection_reason": pending.rejection_reason,
                }
            )
        return Response(
            {
                "status": "pending",
                "clinic_name": tenant.clinic_name,
                "clinic_tin": tenant.clinic_tin,
                "setup_fee_etb": tenant.setup_fee_etb,
            }
        )
