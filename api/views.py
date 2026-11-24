from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from .serializers import (
    ChangePasswordSerializer,
    CustomTokenObtainPairSerializer,
    UserSerializer,
)

User = get_user_model()


@api_view(["GET"])
@permission_classes([permissions.AllowAny])
def health(_request):
    """Liveness for the cloud; on a clinic server the host starter also waits on the database."""
    from django.conf import settings
    from django.db import connection

    node_mode = getattr(settings, "NODE_MODE", "cloud")
    if node_mode != "clinic":
        return Response({"status": "ok"})
    try:
        connection.ensure_connection()
    except Exception:
        return Response({"status": "degraded", "node_mode": node_mode, "database": False}, status=503)
    return Response({"status": "ok", "node_mode": node_mode, "database": True})


class UserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all().order_by("id")
    serializer_class = UserSerializer

    def get_permissions(self):
        if self.action == "create":
            return [permissions.AllowAny()]
        return [permissions.IsAuthenticated()]

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return User.objects.none()
        profile = getattr(user, "profile", None)
        tin = getattr(profile, "clinic_tin", "")
        qs = User.objects.filter(profile__clinic_tin=tin).select_related("profile")
        branch = (getattr(profile, "branch_name", "") or "").strip()
        if branch:
            qs = qs.filter(profile__branch_name__iexact=branch)
        return qs

    # Staff accounts are managed on the cloud and copied to a clinic server by sync (cloud wins), so changes made
    # on the server would be undone by the next sync. Only a password change travels back up.
    def _on_clinic_server(self) -> bool:
        from django.conf import settings

        return getattr(settings, "NODE_MODE", "cloud") == "clinic"

    def create(self, request, *args, **kwargs):
        if self._on_clinic_server():
            return Response(
                {"detail": "Staff accounts are created on the cloud. Add them there, then sync this server."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().create(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        if self._on_clinic_server() and set(request.data.keys()) - {"password"}:
            return Response(
                {"detail": "Roles and access are managed on the cloud. On this server you can only change a password."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        if self._on_clinic_server():
            return Response(
                {"detail": "Staff accounts are removed on the cloud. Remove them there, then sync this server."},
                status=status.HTTP_403_FORBIDDEN,
            )
        actor_profile = getattr(request.user, "profile", None)
        if not actor_profile or (actor_profile.role or "").lower() != "manager":
            return Response(
                {"detail": "Only managers can delete staff accounts."},
                status=status.HTTP_403_FORBIDDEN,
            )
        target = self.get_object()
        if target.id == request.user.id:
            return Response(
                {"detail": "You cannot delete your own manager account."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target_role = (getattr(getattr(target, "profile", None), "role", "") or "").lower()
        if target_role == "manager":
            return Response(
                {"detail": "Manager accounts cannot be deleted from staff tools."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def me(self, request):
        return Response(UserSerializer(request.user).data)

    @action(detail=False, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def change_password(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(serializer.validated_data["old_password"]):
            return Response(
                {"old_password": ["Old password is incorrect."]},
                status=status.HTTP_400_BAD_REQUEST,
            )
        user.set_password(serializer.validated_data["new_password"])
        user.save()
        profile = getattr(user, "profile", None)
        if profile is not None:
            profile.password_changed_at = timezone.now()
            profile.save(update_fields=["password_changed_at", "updated_at"])
        return Response({"detail": "Password updated successfully."})


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
