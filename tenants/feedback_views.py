from django.utils import timezone
from rest_framework import permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TenantFeedbackMessage, TenantFeedbackThread
from .services import resolve_tenant_account

CLOUD_NEEDED = "Apex chat needs the internet. Nothing was sent; try again when the clinic is online."


def clinic_tin_for_user(user) -> str:
    profile = getattr(user, "profile", None)
    return (getattr(profile, "clinic_tin", None) or "").strip()


def unread_apex_count(thread: TenantFeedbackThread | None) -> int:
    if not thread:
        return 0
    return TenantFeedbackMessage.objects.filter(
        thread=thread,
        sender_side=TenantFeedbackMessage.SIDE_APEX,
        read_by_tenant=False,
    ).count()


def serialize_message(message: TenantFeedbackMessage) -> dict:
    return {
        "id": message.id,
        "sender_side": message.sender_side,
        "body": message.body,
        "image_url": message.image_url,
        "sender_username": message.sender_username,
        "created_at": message.created_at,
        "read_by_tenant": message.read_by_tenant,
        "read_by_apex": message.read_by_apex,
    }


# ---- The chat itself. These run where the chat lives: the cloud. ----
def unread_payload(tin: str) -> dict:
    thread = TenantFeedbackThread.objects.filter(pharmacy_tin=tin).first()
    return {"unread_count": unread_apex_count(thread)}


def thread_payload(tin: str) -> dict:
    """The clinic's chat with Apex. Reading it marks Apex's messages as read."""
    tenant = resolve_tenant_account(tin)
    thread, _ = TenantFeedbackThread.objects.get_or_create(pharmacy_tin=tin)
    TenantFeedbackMessage.objects.filter(
        thread=thread,
        sender_side=TenantFeedbackMessage.SIDE_APEX,
        read_by_tenant=False,
    ).update(read_by_tenant=True)
    return {
        "thread": {
            "id": thread.id,
            "clinic_tin": thread.pharmacy_tin,
            "status": thread.status,
            "clinic_name": tenant.clinic_name if tenant else "",
        },
        "messages": [serialize_message(message) for message in thread.messages.all()],
        "unread_count": 0,
    }


def post_message(tin: str, username: str, body: str, image_url: str) -> tuple[int, dict]:
    body, image_url = (body or "").strip(), (image_url or "").strip()
    if not body and not image_url:
        return 400, {"detail": "Message body or image is required."}
    thread, _ = TenantFeedbackThread.objects.get_or_create(pharmacy_tin=tin)
    if thread.status == TenantFeedbackThread.STATUS_CLOSED:
        thread.status = TenantFeedbackThread.STATUS_OPEN
        thread.closed_at = None
        thread.save(update_fields=["status", "closed_at", "updated_at"])
    message = TenantFeedbackMessage.objects.create(
        thread=thread,
        sender_side=TenantFeedbackMessage.SIDE_TENANT,
        body=body,
        image_url=image_url,
        sender_username=username,
        read_by_tenant=True,
        read_by_apex=False,
    )
    thread.updated_at = timezone.now()
    thread.save(update_fields=["updated_at"])
    return 201, serialize_message(message)


def _via_cloud(path: str, payload: dict):
    """On a clinic server the chat lives in the cloud only: forward it, and never store or queue it here.

    Returns None when this is not a clinic server. Otherwise a Response: the cloud's answer, or a 503 with
    code "cloud_unreachable" when the cloud cannot be reached (the chat then stays disabled).
    """
    from clinic import sync_billing
    from clinic.sync_client import TransportError

    if not sync_billing.proxy_enabled():
        return None
    try:
        return Response(sync_billing.cloud_call(path, payload))
    except TransportError as exc:
        if exc.status and exc.status < 500:
            return Response({"detail": str(exc)}, status=exc.status)
        return Response({"detail": CLOUD_NEEDED, "code": "cloud_unreachable"}, status=503)


class ClinicFeedbackUnreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"unread_count": 0})
        proxied = _via_cloud("feedback/unread", {})
        if proxied is not None:
            # This is a polling check, not an action: an unreachable cloud is a state, not an error.
            if proxied.status_code == 503:
                return Response({"unread_count": 0, "cloud_unreachable": True})
            return proxied
        return Response(unread_payload(tin))


class ClinicFeedbackThreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"detail": "Clinic account not found."}, status=404)
        proxied = _via_cloud("feedback/thread", {})
        if proxied is not None:
            return proxied
        return Response(thread_payload(tin))


class ClinicFeedbackSendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"detail": "Clinic account not found."}, status=404)
        payload = {
            "body": request.data.get("body") or "",
            "image_url": request.data.get("image_url") or "",
            "username": request.user.username,
        }
        proxied = _via_cloud("feedback/send", payload)
        if proxied is not None:
            return proxied
        code, data = post_message(tin, request.user.username, payload["body"], payload["image_url"])
        return Response(data, status=code)
