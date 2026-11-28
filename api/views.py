from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView

from . import audit
from . import persons as persons_mod
from .models import AuditEvent, StaffPerson, UserProfile
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

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Role logins created, changed or removed by the manager. Self sign-up (no user yet) is not an audit case.
        if request.user.is_authenticated and getattr(self, "action", None) in {"create", "update", "partial_update", "destroy"}:
            audit.record_from_view(self, request, response)
        return response

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

    # A clinic server manages its own staff offline. Accounts created or edited there travel up on the next sync
    # (see sync_engine.apply_staff_changes); the cloud refuses one only if the username belongs to another clinic.
    def _on_clinic_server(self) -> bool:
        from django.conf import settings

        return getattr(settings, "NODE_MODE", "cloud") == "clinic"

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
        if self._on_clinic_server():
            # Deactivate instead of delete: the account's name stays on past records, and "inactive" is a change
            # the next sync can carry to the cloud (a deleted row leaves nothing to send).
            target.is_active = False
            target.save(update_fields=["is_active"])
            profile = getattr(target, "profile", None)
            if profile is not None:
                profile.is_active = False
                profile.save(update_fields=["is_active", "updated_at"])
            return Response(status=status.HTTP_204_NO_CONTENT)
        target.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=["get"])
    def me(self, request):
        data = UserSerializer(request.user).data
        person = audit._person_from_request(request)
        data["person"] = (
            {"id": person.id, "name": person.full_name, "is_default": person.is_default} if person else None
        )
        return Response(data)

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


class PersonViewSet(viewsets.ViewSet):
    """The people under each role's shared login. Managers only."""

    permission_classes = [permissions.IsAuthenticated]

    def _manager_profile(self, request):
        profile = getattr(request.user, "profile", None)
        if profile is None or (profile.role or "").lower() != "manager":
            raise PermissionDenied("Only managers can manage staff people.")
        return profile

    def _branch_profiles(self, manager):
        return UserProfile.objects.filter(
            clinic_tin=manager.clinic_tin, branch_name__iexact=manager.branch_name, is_active=True
        ).exclude(role__iexact="manager")

    def _row(self, person):
        return {
            "id": person.id,
            "full_name": person.full_name,
            "role": person.profile.role,
            "username": person.profile.user.username,
            "is_default": person.is_default,
            "is_active": person.is_active,
            "created_at": person.created_at,
        }

    def _person(self, manager, pk):
        person = (
            StaffPerson.objects.filter(pk=pk, is_active=True, profile__in=self._branch_profiles(manager))
            .select_related("profile__user")
            .first()
        )
        if person is None:
            raise NotFound("Person not found.")
        return person

    def list(self, request):
        manager = self._manager_profile(request)
        profiles = list(self._branch_profiles(manager).select_related("user"))
        for profile in profiles:
            if profile.user.has_usable_password():
                persons_mod.ensure_default_person(profile)
        people = StaffPerson.objects.filter(is_active=True, profile__in=profiles).select_related("profile__user")
        return Response([self._row(p) for p in people.order_by("profile__role", "-is_default", "full_name")])

    def create(self, request):
        manager = self._manager_profile(request)
        role = (request.data.get("role") or "").strip().lower()
        profile = self._branch_profiles(manager).filter(role__iexact=role).select_related("user").first()
        if profile is None:
            raise ValidationError({"role": ["Create this role's login first, then add people to it."]})
        person = persons_mod.create_person(
            profile,
            request.data.get("full_name"),
            request.data.get("password"),
            request.data.get("confirm_password"),
        )
        audit.record(
            request,
            action="create",
            entity="staff person",
            entity_id=person.id,
            summary=f"Added {person.full_name} to {profile.role}",
            details={"role": profile.role, "full_name": person.full_name},
        )
        return Response(self._row(person), status=status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        manager = self._manager_profile(request)
        person = self._person(manager, pk)
        before = person.full_name
        persons_mod.update_person(
            person,
            full_name=request.data.get("full_name"),
            password=request.data.get("password") or None,
            confirm=request.data.get("confirm_password") if request.data.get("password") else None,
        )
        changed = []
        if person.full_name != before:
            changed.append(f"name {before} → {person.full_name}")
        if request.data.get("password"):
            changed.append("password")
        audit.record(
            request,
            action="update",
            entity="staff person",
            entity_id=person.id,
            summary=f"Changed {person.full_name} ({person.profile.role}): {', '.join(changed) or 'no change'}",
            details={"role": person.profile.role, "changed": changed},
        )
        return Response(self._row(person))

    def destroy(self, request, pk=None):
        manager = self._manager_profile(request)
        person = self._person(manager, pk)
        promote_to = request.query_params.get("promote_to")
        new_default = persons_mod.delete_person(person, int(promote_to) if promote_to else None)
        audit.record(
            request,
            action="delete",
            entity="staff person",
            entity_id=person.id,
            summary=f"Removed {person.full_name} from {person.profile.role}"
            + (f"; {new_default.full_name} is now the default" if new_default else ""),
            details={"role": person.profile.role, "new_default": new_default.full_name if new_default else None},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomTokenObtainPairView(TokenObtainPairView):
    serializer_class = CustomTokenObtainPairSerializer


class AuditViewSet(viewsets.ViewSet):
    """Who did what, in detail. Managers only; each manager sees their own branch."""

    permission_classes = [permissions.IsAuthenticated]
    PAGE = 10

    def _scope(self, request):
        profile = getattr(request.user, "profile", None)
        if profile is None or (profile.role or "").lower() != "manager":
            raise PermissionDenied("Only managers can read the audit log.")
        return AuditEvent.objects.filter(clinic_tin=profile.clinic_tin, branch_name__iexact=profile.branch_name)

    @staticmethod
    def _row(e, detail=False):
        row = {
            "id": e.id,
            "when": e.created_at,
            "actor_name": e.actor_name or e.actor_username,
            "actor_username": e.actor_username,
            "actor_role": e.actor_role,
            "action": e.action,
            "entity": e.entity,
            "entity_id": e.entity_id,
            "summary": e.summary,
        }
        if detail:
            row["details"] = e.details
        return row

    def list(self, request):
        from django.db.models import Q

        qs = self._scope(request)
        params = request.query_params
        q = (params.get("q") or "").strip()
        if q:
            qs = qs.filter(
                Q(actor_name__icontains=q) | Q(actor_username__icontains=q) | Q(summary__icontains=q) | Q(entity__icontains=q)
            )
        if params.get("role"):
            qs = qs.filter(actor_role__iexact=params["role"])
        if params.get("action"):
            qs = qs.filter(action=params["action"])
        if params.get("date_from"):
            qs = qs.filter(created_at__date__gte=params["date_from"])
        if params.get("date_to"):
            qs = qs.filter(created_at__date__lte=params["date_to"])
        try:
            page = max(1, int(params.get("page") or 1))
        except ValueError:
            page = 1
        total = qs.count()
        start = (page - 1) * self.PAGE
        rows = [self._row(e) for e in qs[start:start + self.PAGE]]
        base = self._scope(request)
        return Response(
            {
                "count": total,
                "page": page,
                "page_size": self.PAGE,
                "results": rows,
                "roles": sorted({r for r in base.values_list("actor_role", flat=True).distinct() if r}),
                "actions": sorted({a for a in base.values_list("action", flat=True).distinct() if a}),
            }
        )

    def retrieve(self, request, pk=None):
        event = self._scope(request).filter(pk=pk).first()
        if event is None:
            raise NotFound("Event not found.")
        return Response(self._row(event, detail=True))
