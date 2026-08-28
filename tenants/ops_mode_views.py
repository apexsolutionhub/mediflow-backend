from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TenantAccount, TenantOpsModeChangeRequest

User = get_user_model()
VALID_OPS_MODES = {TenantAccount.OPS_MODE_ONLINE, TenantAccount.OPS_MODE_OFFLINE}


def serialize_ops_mode_request(req: TenantOpsModeChangeRequest) -> dict:
    return {
        "id": req.id,
        "clinic_tin": req.clinic_tin,
        "current_ops_mode": req.current_ops_mode,
        "requested_ops_mode": req.requested_ops_mode,
        "request_note": req.request_note,
        "status": req.status,
        "requested_by_username": req.requested_by_username,
        "review_note": req.review_note,
        "reviewed_at": req.reviewed_at,
        "applied_at": req.applied_at,
        "created_at": req.created_at,
        "updated_at": req.updated_at,
        "applies_immediately": is_immediate_transition(
            req.current_ops_mode, req.requested_ops_mode
        ),
    }


def is_immediate_transition(current: str, requested: str) -> bool:
    """Online → offline applies on Apex approval without a sync step."""
    current = (current or TenantAccount.OPS_MODE_ONLINE).strip().lower()
    requested = (requested or "").strip().lower()
    return (
        current == TenantAccount.OPS_MODE_ONLINE
        and requested == TenantAccount.OPS_MODE_OFFLINE
    )


def active_ops_mode_block(tenant: TenantAccount):
    """Approved offline → online change not yet applied after sync."""
    return (
        TenantOpsModeChangeRequest.objects.filter(
            clinic_tin=tenant.clinic_tin,
            status=TenantOpsModeChangeRequest.STATUS_APPROVED,
            applied_at__isnull=True,
            requested_ops_mode=TenantAccount.OPS_MODE_ONLINE,
        )
        .order_by("-reviewed_at")
        .first()
    )


def apply_ops_mode_to_tenant(tenant: TenantAccount, ops_mode: str) -> None:
    tenant.ops_mode = ops_mode
    tenant.save(update_fields=["ops_mode", "updated_at"])


class OpsModeStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile:
            return Response({"detail": "Profile not found."}, status=status.HTTP_404_NOT_FOUND)
        tenant = TenantAccount.objects.filter(clinic_tin=profile.clinic_tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        pending = (
            TenantOpsModeChangeRequest.objects.filter(
                clinic_tin=tenant.clinic_tin,
                status=TenantOpsModeChangeRequest.STATUS_PENDING,
            )
            .order_by("-created_at")
            .first()
        )
        approved_awaiting_sync = active_ops_mode_block(tenant)
        latest = (
            TenantOpsModeChangeRequest.objects.filter(clinic_tin=tenant.clinic_tin)
            .order_by("-created_at")
            .first()
        )
        recent = TenantOpsModeChangeRequest.objects.filter(
            clinic_tin=tenant.clinic_tin
        ).order_by("-created_at")[:10]

        current = (tenant.ops_mode or TenantAccount.OPS_MODE_ONLINE).strip().lower()
        alternate = (
            TenantAccount.OPS_MODE_OFFLINE
            if current == TenantAccount.OPS_MODE_ONLINE
            else TenantAccount.OPS_MODE_ONLINE
        )

        return Response(
            {
                "ops_mode": tenant.ops_mode,
                "pending_request": serialize_ops_mode_request(pending) if pending else None,
                "approved_awaiting_sync": (
                    serialize_ops_mode_request(approved_awaiting_sync)
                    if approved_awaiting_sync
                    else None
                ),
                "latest_request": serialize_ops_mode_request(latest) if latest else None,
                "recent_requests": [serialize_ops_mode_request(r) for r in recent],
                "can_request_change": pending is None and approved_awaiting_sync is None,
                "next_mode": alternate,
                "next_mode_applies_immediately": is_immediate_transition(current, alternate),
                "next_mode_applies_on_approval_without_sync": is_immediate_transition(
                    current, alternate
                ),
            }
        )


class RequestOpsModeChangeView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        profile = getattr(request.user, "profile", None)
        if not profile or (profile.role or "").strip().lower() != "manager":
            return Response(
                {"detail": "Only clinic managers can request ops mode changes."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tenant = TenantAccount.objects.filter(clinic_tin=profile.clinic_tin).first()
        if not tenant:
            return Response({"detail": "Tenant not found."}, status=status.HTTP_404_NOT_FOUND)

        requested = (request.data.get("requested_ops_mode") or "").strip().lower()
        if requested not in VALID_OPS_MODES:
            return Response(
                {"detail": "requested_ops_mode must be online or offline."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        current = (tenant.ops_mode or TenantAccount.OPS_MODE_ONLINE).strip().lower()
        if requested == current:
            return Response(
                {"detail": "Clinic is already on that operating mode."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if active_ops_mode_block(tenant):
            return Response(
                {
                    "detail": "An approved return-to-cloud is waiting for sync. Complete sync before requesting again.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        existing_pending = TenantOpsModeChangeRequest.objects.filter(
            clinic_tin=tenant.clinic_tin,
            status=TenantOpsModeChangeRequest.STATUS_PENDING,
        )
        if existing_pending.exists():
            return Response(
                {"detail": "A pending ops mode request already exists for this clinic."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        note = (request.data.get("request_note") or "").strip()
        req = TenantOpsModeChangeRequest.objects.create(
            clinic_tin=tenant.clinic_tin,
            current_ops_mode=current,
            requested_ops_mode=requested,
            request_note=note,
            requested_by_username=request.user.username,
            requested_by=request.user,
        )
        return Response(
            {
                "detail": "Ops mode change submitted to Apex for review.",
                "applied_immediately": False,
                "ops_mode": tenant.ops_mode,
                "request": serialize_ops_mode_request(req),
            },
            status=status.HTTP_201_CREATED,
        )
