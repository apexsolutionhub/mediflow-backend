"""Clinic-node side of sync: push local changes to the cloud, then pull the cloud's, in that order.

Each step is recorded on a `SyncRun`. A failed step stops the run; retrying the same run skips a
step that already succeeded (a push is safe to repeat anyway, the cloud ignores a repeated push id).
"""
import json
import urllib.error
import urllib.request
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import SyncRun, SyncState
from .sync_billing import export_local_submissions, mirror_submission_status
from .sync_engine import (
    PULL,
    Scope,
    SyncError,
    apply_remap,
    apply_scope,
    apply_staff,
    store_remote_conflicts,
    apply_tenant,
    export_credentials,
    export_scope,
)

PUSH_MARGIN = timedelta(minutes=5)  # re-send a little before the last run so a clock hiccup loses nothing


TOKEN_REJECTED_MESSAGE = (
    "The cloud no longer accepts this server's sync token (it was revoked or replaced). "
    "Ask Apex for a new token, put it in NODE_TOKEN in BackEnd/.env on this PC, then restart the clinic server."
)


class TransportError(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


class HttpTransport:
    """Talks to the cloud API over HTTPS with the node token."""

    def __init__(self, base_url: str, token: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _post(self, path, body):
        request = urllib.request.Request(
            f"{self.base_url}/sync/{path}/",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Node {self.token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("detail", "")
            except Exception:
                detail = ""
            if exc.code in (401, 403):
                # Only a token problem looks like this: say what it is and who can fix it.
                raise TransportError(TOKEN_REJECTED_MESSAGE, exc.code, code="token_rejected") from exc
            raise TransportError(detail or f"The cloud answered {exc.code}.", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError("The cloud is unreachable. Check the internet connection.") from exc

    def push(self, body):
        return self._post("push", body)

    def pull(self, body):
        return self._post("pull", body)

    def complete(self, body):
        return self._post("complete", body)

    def call(self, path, body):
        """Any other cloud call, e.g. "billing/status"."""
        return self._post(path, body)


class LocalTransport:
    """Calls the cloud handlers in-process (tests, and a node and cloud sharing one process)."""

    def __init__(self, node, cloud_db="default"):
        from . import sync_cloud

        self.node, self.cloud_db, self.cloud = node, cloud_db, sync_cloud

    def _call(self, handler, body):
        status, payload = handler(self.node, body, db=self.cloud_db)
        if status >= 400:
            raise TransportError(payload.get("detail", "Rejected."), status)
        return payload

    def push(self, body):
        return self._call(self.cloud.handle_push, body)

    def pull(self, body):
        return self._call(self.cloud.handle_pull, body)

    def complete(self, body):
        return self._call(self.cloud.handle_complete, body)

    def call(self, path, body):
        handlers = {
            "billing/status": self.cloud.handle_billing_status,
            "billing/submit": self.cloud.handle_billing_submit,
            "feedback/unread": self.cloud.handle_feedback_unread,
            "feedback/thread": self.cloud.handle_feedback_thread,
            "feedback/send": self.cloud.handle_feedback_send,
        }
        return self._call(handlers[path], body)


def _last_push_since(scope, db, current):
    """Start of the last run that fully succeeded (minus a margin); None means send everything."""
    last = (
        SyncRun.objects.using(db)
        .filter(
            clinic_tin=scope.tin,
            branch_name=scope.branch,
            push_status__in=[SyncRun.STEP_OK, SyncRun.STEP_SKIPPED],
            pull_status=SyncRun.STEP_OK,
        )
        .exclude(pk=current.pk)
        .order_by("-started_at")
        .first()
    )
    return last.started_at - PUSH_MARGIN if last else None


def _presync_backup(run: SyncRun, db: str):
    """Back up the clinic database right before a sync, so a bad pull can always be undone.

    A failed backup does not stop the sync (a broken backup tool must not lock the clinic out through
    the freshness rule); it is recorded on the run and shown to the manager.
    """
    from django.conf import settings

    if db != "default" or run.kind != SyncRun.KIND_SYNC or not getattr(settings, "BACKUP_BEFORE_SYNC", False):
        return
    from . import backup

    result = backup.make_backup("presync")
    run.receipt = {
        **run.receipt,
        "backup": {"ok": result.ok, "file": result.path.name if result.path else "", "error": result.error},
    }
    run.save(using=db)


def run_sync(scope: Scope, transport, user=None, kind=SyncRun.KIND_SYNC, run=None, db="default") -> SyncRun:
    """Push, then pull, then tell the cloud it is done. Never raises: the outcome is on the run."""
    state, _ = SyncState.objects.using(db).get_or_create(clinic_tin=scope.tin, branch_name=scope.branch)
    if run is None:
        run = SyncRun.objects.using(db).create(
            clinic_tin=scope.tin,
            branch_name=scope.branch,
            kind=kind,
            started_by=user if getattr(user, "is_authenticated", False) else None,
        )
    else:
        kind = run.kind
        run.error_message = ""
    step = "push"
    try:
        # 1. Push local changes (a seed has nothing to push: the node starts empty).
        if run.push_status not in (SyncRun.STEP_OK, SyncRun.STEP_SKIPPED):
            if kind == SyncRun.KIND_SEED:
                run.push_status = SyncRun.STEP_SKIPPED
                run.save(using=db)
            else:
                run.push_status = SyncRun.STEP_RUNNING
                run.save(using=db)
                _presync_backup(run, db)
                since = _last_push_since(scope, db, run)
                receipt = transport.push(
                    {
                        "push_id": str(run.sync_uuid),
                        "kind": kind,
                        "since_token": state.last_sync_token or None,
                        "entities": export_scope(scope, since=since, db=db),
                        "billing_submissions": export_local_submissions(scope.tin, db),
                        "staff_credentials": export_credentials(scope, since, db),
                    }
                )
                apply_remap(receipt.get("remap") or {}, db)
                mirror_submission_status(scope.tin, receipt.get("billing_verdicts"), db)
                store_remote_conflicts(scope, receipt.get("conflict_items"), db)
                run.push_status = SyncRun.STEP_OK
                run.receipt = {**run.receipt, "push": receipt}
                run.save(using=db)

        # 2. Pull the cloud's changes, only after the push succeeded.
        step = "pull"
        run.pull_status = SyncRun.STEP_RUNNING
        run.save(using=db)
        pulled = transport.pull(
            {"kind": kind, "since_token": None if kind == SyncRun.KIND_SEED else (state.last_sync_token or None)}
        )
        with transaction.atomic(using=db):
            result = apply_scope(scope, pulled.get("entities") or {}, PULL, db=db, local_cutoff=run.started_at)
            apply_staff(scope, pulled.get("staff") or [], db=db)
        done = transport.complete({"kind": kind, "token": pulled["token"], "run_id": str(run.sync_uuid)})
        apply_tenant(scope.tin, done.get("tenant") or pulled.get("tenant"), db=db)

        # 3. Complete.
        now = timezone.now()
        state.last_successful_sync_at = now
        state.last_sync_token = pulled["token"]
        state.restore_pending = False
        if kind == SyncRun.KIND_SEED:
            state.seeded_at = now
        state.save(using=db)
        run.pull_status = SyncRun.STEP_OK
        run.finished_at = now
        run.receipt = {**run.receipt, "pull": result.as_dict()}
        run.save(using=db)
    except (TransportError, SyncError) as exc:
        if step == "push":
            run.push_status = SyncRun.STEP_FAILED
        else:
            run.pull_status = SyncRun.STEP_FAILED
        run.error_message = str(exc)[:2000]
        run.finished_at = timezone.now()
        run.save(using=db)
    return run
