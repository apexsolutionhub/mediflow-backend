"""Sync API.

Cloud (called by a clinic node with its node token):  POST push/, pull/, complete/
Manager (JWT):  GET status/, GET conflicts/  on either side;  POST run/, seed/  on a clinic node.
"""
from django.conf import settings
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from . import backup
from .models import SyncConflict, SyncRun, SyncState
from .sync_client import HttpTransport, run_sync
from .sync_cloud import (
    IsSyncNode,
    NodeTokenAuthentication,
    handle_complete,
    handle_pull,
    handle_push,
)
from .sync_billing import handle_billing_status, handle_billing_submit
from .sync_engine import Scope


def _is_node() -> bool:
    return getattr(settings, "NODE_MODE", "cloud") == "clinic"


def _manager_scope(request):
    profile = getattr(request.user, "profile", None)
    if profile is None or (profile.role or "").strip().lower() != "manager":
        return None
    return Scope((profile.clinic_tin or "").strip(), (profile.branch_name or "").strip() or "Main")


class _NodeCall(APIView):
    """Calls from a clinic node. They only make sense on the cloud."""

    authentication_classes = [NodeTokenAuthentication]
    permission_classes = [IsSyncNode]
    handler = None

    def post(self, request):
        if _is_node():
            return Response({"detail": "A clinic node does not accept sync calls."}, status=status.HTTP_400_BAD_REQUEST)
        code, payload = type(self).handler(request.auth, request.data)
        return Response(payload, status=code)


class SyncPushView(_NodeCall):
    handler = staticmethod(handle_push)


class SyncPullView(_NodeCall):
    handler = staticmethod(handle_pull)


class SyncCompleteView(_NodeCall):
    handler = staticmethod(handle_complete)


class SyncBillingStatusView(_NodeCall):
    handler = staticmethod(handle_billing_status)


class SyncBillingSubmitView(_NodeCall):
    handler = staticmethod(handle_billing_submit)


def _run_json(run: SyncRun) -> dict:
    return {
        "id": str(run.sync_uuid),
        "kind": run.kind,
        "push_status": run.push_status,
        "pull_status": run.pull_status,
        "error_message": run.error_message,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "receipt": run.receipt,
    }


class SyncStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can see sync."}, status=status.HTTP_403_FORBIDDEN)
        from tenants.ops_mode_views import active_ops_mode_block, awaiting_step
        from tenants.services import resolve_tenant_account

        state = SyncState.objects.filter(clinic_tin=scope.tin, branch_name=scope.branch).first()
        runs = SyncRun.objects.filter(clinic_tin=scope.tin, branch_name=scope.branch).order_by("-started_at")[:10]
        tenant = resolve_tenant_account(scope.tin)
        waiting = active_ops_mode_block(tenant) if tenant else None
        return Response(
            {
                "node_mode": "clinic" if _is_node() else "cloud",
                "ops_mode": getattr(tenant, "ops_mode", None),
                "can_sync": _is_node(),
                "seeded_at": state.seeded_at if state else None,
                "last_successful_sync_at": state.last_successful_sync_at if state else None,
                "awaiting_step": awaiting_step(waiting) if waiting else None,
                "backup": backup.backup_status() if _is_node() else None,
                "open_conflicts": SyncConflict.objects.filter(
                    clinic_tin=scope.tin, branch_name=scope.branch, resolved_at__isnull=True
                ).count(),
                "runs": [_run_json(r) for r in runs],
            }
        )


class SyncConflictsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can see sync."}, status=status.HTTP_403_FORBIDDEN)
        rows = SyncConflict.objects.filter(
            clinic_tin=scope.tin, branch_name=scope.branch, resolved_at__isnull=True
        )[:100]
        return Response(
            [
                {
                    "id": c.id,
                    "entity": c.entity,
                    "label": c.label,
                    "side": c.side,
                    "note": c.note,
                    "incoming": c.incoming,
                    "existing": c.existing,
                    "created_at": c.created_at,
                }
                for c in rows
            ]
        )


class _NodeRun(APIView):
    """Manager-started sync on a clinic node."""

    permission_classes = [permissions.IsAuthenticated]
    kind = SyncRun.KIND_SYNC

    def post(self, request):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can run sync."}, status=status.HTTP_403_FORBIDDEN)
        if not _is_node():
            return Response(
                {"detail": "Sync runs on the clinic server. This is the cloud."}, status=status.HTTP_400_BAD_REQUEST
            )
        if not (settings.CLOUD_API_URL and settings.NODE_TOKEN):
            return Response(
                {"detail": "This clinic server is not linked to the cloud yet (CLOUD_API_URL / NODE_TOKEN)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        run = None
        run_id = request.data.get("run_id")
        if run_id:
            run = SyncRun.objects.filter(
                sync_uuid=run_id, clinic_tin=scope.tin, branch_name=scope.branch
            ).first()
            if run is None:
                return Response({"detail": "Sync run not found."}, status=status.HTTP_404_NOT_FOUND)
        run = run_sync(
            scope,
            HttpTransport(settings.CLOUD_API_URL, settings.NODE_TOKEN),
            user=request.user,
            kind=self.kind,
            run=run,
        )
        failed = SyncRun.STEP_FAILED in (run.push_status, run.pull_status)
        return Response(_run_json(run), status=status.HTTP_502_BAD_GATEWAY if failed else status.HTTP_200_OK)


class BackupNowView(APIView):
    """Manager presses "Back up now" on a clinic server."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if _manager_scope(request) is None:
            return Response({"detail": "Only clinic managers can back up."}, status=status.HTTP_403_FORBIDDEN)
        if not _is_node():
            return Response(
                {"detail": "Backups are made on the clinic server. This is the cloud."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        result = backup.make_backup("manual")
        body = backup.backup_status()
        if not result.ok:
            return Response({"detail": result.error, "backup": body}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"backup": body})


class SyncRunView(_NodeRun):
    kind = SyncRun.KIND_SYNC


class SyncSeedView(_NodeRun):
    kind = SyncRun.KIND_SEED
