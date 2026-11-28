"""Sync API.

Cloud (called by a clinic node with its node token):  POST push/, pull/, complete/
Manager (JWT):  GET status/, GET conflicts/  on either side;  POST run/, seed/  on a clinic node.
"""
from django.conf import settings
from django.db.models import Q
from django.utils import timezone
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
from .sync_feedback import handle_feedback_send, handle_feedback_thread, handle_feedback_unread
from .sync_gate import sync_reminder


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


class SyncFeedbackUnreadView(_NodeCall):
    handler = staticmethod(handle_feedback_unread)


class SyncFeedbackThreadView(_NodeCall):
    handler = staticmethod(handle_feedback_thread)


class SyncFeedbackSendView(_NodeCall):
    handler = staticmethod(handle_feedback_send)


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


STALE_RUN_MINUTES = 10  # three cloud calls of at most 2 minutes each, with room to spare


def expire_stale_runs(scope):
    """A run still "running" long after it began lost its worker (the server restarted, the PC slept).

    Mark it failed so the page stops waiting on it and the manager can run again.
    """
    from datetime import timedelta

    cutoff = timezone.now() - timedelta(minutes=STALE_RUN_MINUTES)
    stale = SyncRun.objects.filter(
        clinic_tin=scope.tin, branch_name=scope.branch, finished_at__isnull=True, started_at__lt=cutoff
    ).filter(Q(push_status=SyncRun.STEP_RUNNING) | Q(pull_status=SyncRun.STEP_RUNNING))
    for run in stale:
        if run.push_status == SyncRun.STEP_RUNNING:
            run.push_status = SyncRun.STEP_FAILED
        else:
            run.pull_status = SyncRun.STEP_FAILED
        run.error_message = "This sync was interrupted (the clinic server restarted or lost its connection). Run it again."
        run.finished_at = timezone.now()
        run.save(update_fields=["push_status", "pull_status", "error_message", "finished_at"])


def _idle_check(scope, state, tenant):
    """The early warning (P19): nothing to push and the due date has not arrived, so a sync is not needed yet.

    Only the pushing side can be counted without contacting the cloud, so the check says what it looked at.
    The manager can still run the sync anyway.
    """
    from tenants.models import TenantAccount

    from .sync_engine import Scope as EngineScope
    from .sync_engine import count_changes, delta_since
    from .sync_gate import allowed_days

    if not _is_node() or tenant is None or (getattr(tenant, "ops_mode", "") or "").lower() != TenantAccount.OPS_MODE_OFFLINE:
        return {"applies": False, "warn_only": False}
    last = state.last_successful_sync_at if state else None
    allowed = allowed_days(tenant)
    days = (timezone.now().date() - last.date()).days if last else None
    due_reached = last is None or days >= allowed
    pending = None
    if state is not None and state.seeded_at and state.last_sync_token:
        pending = count_changes(EngineScope(scope.tin, scope.branch), since=delta_since(state.last_sync_token))
    nothing = pending == 0
    return {
        "applies": True,
        "pending_push": pending,
        "nothing_to_push": nothing,
        "pull_checked": False,
        "due_after_days": allowed,
        "days_since_sync": days,
        "next_due_in_days": None if due_reached else max(allowed - days, 0),
        "due_reached": due_reached,
        "warn_only": bool(nothing and not due_reached),
    }


class SyncStatusView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can see sync."}, status=status.HTTP_403_FORBIDDEN)
        from tenants.ops_mode_views import active_ops_mode_block, awaiting_step
        from tenants.services import resolve_tenant_account

        expire_stale_runs(scope)

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
                "freshness": sync_reminder(tenant, scope.branch),
                "idle": _idle_check(scope, state, tenant),
                "backup": backup.backup_status() if _is_node() else None,
                "open_conflicts": SyncConflict.objects.filter(
                    clinic_tin=scope.tin, branch_name=scope.branch, resolved_at__isnull=True
                ).count(),
                "runs": [_run_json(r) for r in runs],
            }
        )


def _differences(conflict: SyncConflict) -> list:
    """The fields that differ, as "this server" against "the cloud"."""
    if conflict.side == SyncConflict.SIDE_CLOUD:
        server, cloud = conflict.incoming, conflict.existing  # the cloud kept its own copy
    else:
        server, cloud = conflict.existing, conflict.incoming  # this server kept its own copy
    names = [n for n in dict.fromkeys([*server.keys(), *cloud.keys()]) if str(server.get(n)) != str(cloud.get(n))]
    return [{"field": n, "server": server.get(n), "cloud": cloud.get(n)} for n in names]


def _conflict_json(c: SyncConflict) -> dict:
    return {
        "id": c.id,
        "entity": c.entity,
        "label": c.label,
        "side": c.side,
        # Which copy is live on both sides now: the cloud copy after a push conflict, the server copy after a pull one.
        "kept": "cloud" if c.side == SyncConflict.SIDE_CLOUD else "server",
        "note": c.note,
        "differences": _differences(c),
        "created_at": c.created_at,
        "resolved_at": c.resolved_at,
        "resolved_by": c.resolved_by,
        "resolution_note": c.resolution_note,
    }


class SyncConflictsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can see sync."}, status=status.HTTP_403_FORBIDDEN)
        rows = SyncConflict.objects.filter(clinic_tin=scope.tin, branch_name=scope.branch)
        if request.query_params.get("include") != "resolved":
            rows = rows.filter(resolved_at__isnull=True)
        return Response([_conflict_json(c) for c in rows[:100]])


class SyncConflictResolveView(APIView):
    """The manager has looked at a conflict (and re-entered the other version by hand if it mattered)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        scope = _manager_scope(request)
        if scope is None:
            return Response({"detail": "Only clinic managers can review conflicts."}, status=status.HTTP_403_FORBIDDEN)
        conflict = SyncConflict.objects.filter(pk=pk, clinic_tin=scope.tin, branch_name=scope.branch).first()
        if conflict is None:
            return Response({"detail": "Conflict not found."}, status=status.HTTP_404_NOT_FOUND)
        if conflict.resolved_at is not None:
            return Response({"detail": "This conflict is already reviewed."}, status=status.HTTP_400_BAD_REQUEST)
        conflict.resolved_at = timezone.now()
        conflict.resolved_by = request.user.get_username()[:150]
        conflict.resolution_note = str(request.data.get("note") or "").strip()[:255]
        conflict.save(update_fields=["resolved_at", "resolved_by", "resolution_note"])
        return Response(_conflict_json(conflict))


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
                {
                    "detail": "This clinic server has no sync token yet. Ask Apex for one, put it in NODE_TOKEN "
                    "in BackEnd/.env on this PC (with CLOUD_API_URL), then restart the clinic server."
                },
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
        transport = HttpTransport(settings.CLOUD_API_URL, settings.NODE_TOKEN)
        if request.data.get("background"):
            return _start_in_background(request, scope, transport, run, self.kind)
        run = run_sync(scope, transport, user=request.user, kind=self.kind, run=run)
        failed = SyncRun.STEP_FAILED in (run.push_status, run.pull_status)
        return Response(_run_json(run), status=status.HTTP_502_BAD_GATEWAY if failed else status.HTTP_200_OK)


def _start_in_background(request, scope, transport, run, kind):
    """Start the sync on a worker thread and answer at once; the page follows it through /sync/status/.

    The manager can keep working (or leave the page) while the cloud answers, which can take
    a while on a slow link. One sync at a time per branch.
    """
    import threading

    from django.db import connection
    from django.utils import timezone

    expire_stale_runs(scope)
    busy = SyncRun.objects.filter(
        clinic_tin=scope.tin,
        branch_name=scope.branch,
        finished_at__isnull=True,
    ).filter(Q(push_status=SyncRun.STEP_RUNNING) | Q(pull_status=SyncRun.STEP_RUNNING)).first()
    if busy is not None and (run is None or busy.pk != run.pk):
        return Response(_run_json(busy), status=status.HTTP_202_ACCEPTED)
    if run is None:
        run = SyncRun.objects.create(
            clinic_tin=scope.tin, branch_name=scope.branch, kind=kind, started_by=request.user
        )
    run.push_status = SyncRun.STEP_RUNNING
    run.save(update_fields=["push_status"])
    user = request.user

    def work():
        try:
            run_sync(scope, transport, user=user, kind=kind, run=run)
        except Exception as exc:  # run_sync records expected failures; this is the unexpected rest
            SyncRun.objects.filter(pk=run.pk).update(
                push_status=SyncRun.STEP_FAILED, error_message=str(exc)[:2000], finished_at=timezone.now()
            )
        finally:
            connection.close()

    threading.Thread(target=work, daemon=True).start()
    return Response(_run_json(run), status=status.HTTP_202_ACCEPTED)


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
