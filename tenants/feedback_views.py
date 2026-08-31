from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import TenantAccount, TenantFeedbackMessage, TenantFeedbackThread


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


class ClinicFeedbackUnreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"unread_count": 0})
        thread = TenantFeedbackThread.objects.filter(pharmacy_tin=tin).first()
        return Response({"unread_count": unread_apex_count(thread)})


class ClinicFeedbackThreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"detail": "Clinic account not found."}, status=404)

        tenant = TenantAccount.objects.filter(clinic_tin=tin).first()
        thread, _ = TenantFeedbackThread.objects.get_or_create(pharmacy_tin=tin)
        TenantFeedbackMessage.objects.filter(
            thread=thread,
            sender_side=TenantFeedbackMessage.SIDE_APEX,
            read_by_tenant=False,
        ).update(read_by_tenant=True)

        messages = thread.messages.all()
        return Response(
            {
                "thread": {
                    "id": thread.id,
                    "clinic_tin": thread.pharmacy_tin,
                    "status": thread.status,
                    "clinic_name": tenant.clinic_name if tenant else "",
                },
                "messages": [serialize_message(message) for message in messages],
                "unread_count": 0,
            }
        )


class ClinicFeedbackSendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        tin = clinic_tin_for_user(request.user)
        if not tin:
            return Response({"detail": "Clinic account not found."}, status=404)

        body = (request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "Message body is required."}, status=400)

        thread, _ = TenantFeedbackThread.objects.get_or_create(pharmacy_tin=tin)
        if thread.status == TenantFeedbackThread.STATUS_CLOSED:
            thread.status = TenantFeedbackThread.STATUS_OPEN
            thread.closed_at = None
            thread.save(update_fields=["status", "closed_at", "updated_at"])

        message = TenantFeedbackMessage.objects.create(
            thread=thread,
            sender_side=TenantFeedbackMessage.SIDE_TENANT,
            body=body,
            image_url=(request.data.get("image_url") or "").strip(),
            sender_username=request.user.username,
            read_by_tenant=True,
            read_by_apex=False,
        )
        thread.updated_at = timezone.now()
        thread.save(update_fields=["updated_at"])

        return Response(serialize_message(message), status=status.HTTP_201_CREATED)
