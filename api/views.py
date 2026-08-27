from django.contrib.auth import get_user_model
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
    return Response({"status": "ok"})


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
        tin = getattr(getattr(user, "profile", None), "clinic_tin", "")
        return User.objects.filter(profile__clinic_tin=tin).select_related("profile")

    def destroy(self, request, *args, **kwargs):
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
        return Response({"detail": "Password updated successfully."})


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer
