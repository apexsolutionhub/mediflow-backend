"""Cloud side of sync: what a clinic node's push, pull and complete calls do.

The views in `sync_views.py` are thin wrappers around these functions, which also lets tests
drive a "node" and a "cloud" database in one process without HTTP.
"""
import hashlib
import secrets
import uuid

from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .models import SyncNode, SyncRun, SyncState
from .sync_billing import handle_billing_status, handle_billing_submit, receive_submissions
from .sync_feedback import handle_feedback_send, handle_feedback_thread, handle_feedback_unread
from .sync_engine import (
    PUSH,
    Scope,
    SyncError,
    _when,
    apply_credentials,
    apply_staff_changes,
    apply_scope,
    delta_since,
    delta_token,
    export_scope,
    export_staff,
    export_tenant,
)

TOKEN_PREFIX = "mfn_"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_node(tin: str, branch: str, label: str = "") -> tuple[SyncNode, str]:
    """Create a node credential. The token is returned once; only its hash is stored."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    node = SyncNode.objects.create(
        clinic_tin=tin,
        branch_name=branch,
        label=label,
        token_hash=hash_token(token),
        token_prefix=token[:8],
    )
    return node, token


class NodeTokenAuthentication(BaseAuthentication):
    """`Authorization: Node <token>`: the machine credential a clinic node uses to sync."""

    keyword = "Node"

    def authenticate(self, request):
        parts = request.META.get("HTTP_AUTHORIZATION", "").split()
        if len(parts) != 2 or parts[0] != self.keyword:
            return None
        node = SyncNode.objects.filter(token_hash=hash_token(parts[1]), is_active=True).first()
        if node is None:
            raise AuthenticationFailed("Invalid node token.")
        node.last_seen_at = timezone.now()
        node.save(update_fields=["last_seen_at"])
        return (AnonymousUser(), node)

    def authenticate_header(self, request):
        return self.keyword


class IsSyncNode(BasePermission):
    def has_permission(self, request, view):
        return isinstance(request.auth, SyncNode)


def scope_of(node: SyncNode) -> Scope:
    return Scope(node.clinic_tin, node.branch_name)


def handle_push(node: SyncNode, body: dict, db="default") -> tuple[int, dict]:
    """Merge what a node pushed. Safe to repeat: the same `push_id` returns the first receipt."""
    scope = scope_of(node)
    try:
        push_id = uuid.UUID(str(body["push_id"]))
    except (KeyError, ValueError):
        return 400, {"detail": "push_id is required."}
    run, _ = SyncRun.objects.using(db).get_or_create(
        sync_uuid=push_id,
        defaults={
            "clinic_tin": scope.tin,
            "branch_name": scope.branch,
            "kind": body.get("kind") or SyncRun.KIND_SYNC,
            "push_status": SyncRun.STEP_RUNNING,
        },
    )
    if run.clinic_tin != scope.tin or run.branch_name != scope.branch:
        return 403, {"detail": "This push id belongs to another clinic."}
    if run.push_status == SyncRun.STEP_OK:
        return 200, run.receipt
    try:
        result = apply_scope(
            scope, body.get("entities") or {}, PUSH, db=db, since_token=_when(body.get("since_token"))
        )
    except SyncError as exc:
        run.push_status, run.error_message, run.finished_at = SyncRun.STEP_FAILED, str(exc), timezone.now()
        run.save(using=db, update_fields=["push_status", "error_message", "finished_at"])
        return 422, {"detail": str(exc)}
    run.push_status, run.error_message, run.finished_at = SyncRun.STEP_OK, "", timezone.now()
    run.receipt = {
        **result.as_dict(),
        "billing_verdicts": receive_submissions(scope.tin, body.get("billing_submissions") or []),
        # Staff first, so a password changed for a brand-new account lands on the account just created.
        "staff_changes": apply_staff_changes(scope, body.get("staff_changes") or [], db=db),
        "staff_credentials": apply_credentials(scope, body.get("staff_credentials") or [], db=db),
    }
    run.save(using=db, update_fields=["push_status", "error_message", "finished_at", "receipt"])
    return 200, run.receipt


def handle_pull(node: SyncNode, body: dict, db="default") -> tuple[int, dict]:
    """Everything the cloud has for the node's branch (or only what changed after `since_token`)."""
    scope = scope_of(node)
    token = delta_token()
    return 200, {
        "token": token,
        "entities": export_scope(scope, since=delta_since(body.get("since_token")), db=db),
        "staff": export_staff(scope, db=db),
        "tenant": export_tenant(scope.tin),
    }


def handle_complete(node: SyncNode, body: dict, db="default") -> tuple[int, dict]:
    """The node applied its pull: record the sync and apply an approved mode change that was waiting on it."""
    from tenants.ops_mode_views import active_ops_mode_block, awaiting_step, complete_ops_mode_switch
    from tenants.services import resolve_tenant_account

    scope = scope_of(node)
    kind = body.get("kind") or SyncRun.KIND_SYNC
    now = timezone.now()
    state, _ = SyncState.objects.using(db).get_or_create(clinic_tin=scope.tin, branch_name=scope.branch)
    state.last_successful_sync_at = now
    state.last_sync_token = str(body.get("token") or "")[:128]
    if kind == SyncRun.KIND_SEED:
        state.seeded_at = now
    state.save(using=db)

    applied = False
    tenant = resolve_tenant_account(scope.tin)
    if tenant is not None:
        waiting = active_ops_mode_block(tenant)
        if waiting is not None and awaiting_step(waiting) == kind:
            applied = complete_ops_mode_switch(tenant) is not None
    return 200, {"applied_ops_mode_change": applied, "tenant": export_tenant(scope.tin)}
