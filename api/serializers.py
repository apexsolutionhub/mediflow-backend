from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from tenants.billing import PAYMENT_CHANNELS, billing_snapshot, catalog_default_fees, create_payment_submission, resolve_login_access
from tenants.models import TenantAccount
from tenants.services import ensure_tenant_account, generate_clinic_tin, resolve_tenant_account

from . import audit
from .models import UserProfile
from .persons import ensure_default_person, identify

User = get_user_model()
STAFF_ROLES = {"reception", "doctor", "nurse", "lab", "radiology", "pharmacist"}


class UserSerializer(serializers.ModelSerializer):
    clinic_name = serializers.CharField(allow_blank=True, required=False)
    clinic_tin = serializers.CharField(allow_blank=True, required=False)
    role = serializers.CharField(allow_blank=True, required=False)
    logoUrl = serializers.CharField(allow_blank=True, required=False)
    branch_name = serializers.CharField(allow_blank=True, required=False)
    branch_address = serializers.CharField(write_only=True, required=False, allow_blank=True)
    is_main_branch = serializers.BooleanField(write_only=True, required=False, default=True)
    password = serializers.CharField(write_only=True, required=False)
    payment_channel = serializers.CharField(write_only=True, required=False, allow_blank=True)
    payment_transaction_ref = serializers.CharField(write_only=True, required=False, allow_blank=True)
    sales_agent_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    ops_mode = serializers.CharField(write_only=True, required=False, allow_blank=True)
    person_name = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "person_name",
            "id",
            "username",
            "password",
            "clinic_name",
            "clinic_tin",
            "role",
            "logoUrl",
            "branch_name",
            "branch_address",
            "is_main_branch",
            "is_active",
            "payment_channel",
            "payment_transaction_ref",
            "sales_agent_id",
            "ops_mode",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        profile = getattr(instance, "profile", None)
        if profile:
            data["clinic_name"] = profile.clinic_name
            data["clinic_tin"] = profile.clinic_tin
            data["role"] = profile.role
            data["logoUrl"] = profile.logoUrl
            data["branch_name"] = profile.branch_name
            data["is_active"] = profile.is_active
        return data

    def validate_username(self, value):
        username = (value or "").strip()
        if not username:
            raise serializers.ValidationError("Username is required.")
        qs = User.objects.filter(username__iexact=username)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This username is already taken.")
        return username

    def validate(self, attrs):
        request = self.context.get("request")
        is_self_signup = not (request and getattr(request.user, "is_authenticated", False))
        if is_self_signup:
            tin = (attrs.get("clinic_tin") or "").strip()
            # A blank TIN is allowed: create() generates a unique one.
            if tin and TenantAccount.objects.filter(clinic_tin=tin).exists():
                raise serializers.ValidationError({"clinic_tin": ["A clinic with this TIN is already registered."]})
            branch = (attrs.get("branch_name") or "").strip()
            if not branch:
                raise serializers.ValidationError({"branch_name": ["Branch name is required."]})
            channel = (attrs.get("payment_channel") or "").strip()
            ref = (attrs.get("payment_transaction_ref") or "").strip()
            setup_fee = int(catalog_default_fees().get("setup_fee_etb") or 0)
            if setup_fee > 0:
                if channel not in PAYMENT_CHANNELS:
                    raise serializers.ValidationError(
                        {"payment_channel": ["Select Telebirr or Commercial Bank of Ethiopia."]}
                    )
                if len(ref) < 4:
                    raise serializers.ValidationError(
                        {"payment_transaction_ref": ["Transfer ID must be at least 4 characters."]}
                    )
            mode = (attrs.get("ops_mode") or "").strip().lower()
            if mode and mode not in {"online", "offline", "offline_sync", "hybrid"}:
                raise serializers.ValidationError(
                    {"ops_mode": ["Choose online or offline with sync."]}
                )
        return attrs

    def create(self, validated_data):
        request = self.context.get("request")
        is_self_signup = not (request and getattr(request.user, "is_authenticated", False))
        payment_channel = (validated_data.pop("payment_channel", "") or "").strip()
        payment_transaction_ref = (validated_data.pop("payment_transaction_ref", "") or "").strip()
        sales_agent_id = validated_data.pop("sales_agent_id", None)
        ops_mode = validated_data.pop("ops_mode", None)
        branch_address = (validated_data.pop("branch_address", "") or "").strip()
        is_main_branch = validated_data.pop("is_main_branch", True)
        if is_main_branch is None:
            is_main_branch = True
        password = validated_data.pop("password", None)

        if is_self_signup:
            role = "manager"
            clinic_tin = (validated_data.pop("clinic_tin", "") or "").strip() or generate_clinic_tin()
        else:
            actor = request.user.profile
            if (actor.role or "").lower() != "manager":
                raise serializers.ValidationError("Only managers can create staff.")
            role = (validated_data.pop("role", "") or "").strip().lower()
            if role not in STAFF_ROLES:
                raise serializers.ValidationError({"role": ["Choose a clinic staff role."]})
            clinic_tin = actor.clinic_tin
            actor_branch = (actor.branch_name or "").strip() or "Main"
            if UserProfile.objects.filter(
                clinic_tin=clinic_tin,
                branch_name__iexact=actor_branch,
                role__iexact=role,
                is_active=True,
            ).exists():
                raise serializers.ValidationError(
                    {
                        "role": [
                            f"A {role} credential already exists for this branch. "
                            "Add people to it instead of creating another."
                        ]
                    }
                )

        clinic_name = (validated_data.pop("clinic_name", "") or "").strip()
        logo = (validated_data.pop("logoUrl", "") or "").strip()
        branch_name = (validated_data.pop("branch_name", "") or "Main").strip() or "Main"
        if not is_self_signup:
            clinic_name = request.user.profile.clinic_name
            logo = logo or request.user.profile.logoUrl
            branch_name = request.user.profile.branch_name
        profile_data = {
            "clinic_name": clinic_name,
            "role": role,
            "logoUrl": logo,
            "clinic_tin": clinic_tin,
            "branch_name": branch_name,
        }

        user = User(username=validated_data["username"])
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        if password:
            profile_data["password_changed_at"] = timezone.now()
        profile = UserProfile.objects.create(user=user, **profile_data)
        if not is_self_signup and password:
            # The password set here belongs to the role's first (default) person.
            ensure_default_person(profile, validated_data.pop("person_name", ""))

        if is_self_signup:
            from tenants.sales_agents import resolve_sales_agent

            tenant = ensure_tenant_account(
                clinic_tin=clinic_tin,
                clinic_name=profile_data["clinic_name"],
                logo_url=profile_data["logoUrl"],
                branch_name=profile_data["branch_name"],
                branch_address=branch_address,
                is_main_branch=bool(is_main_branch),
                sales_agent=resolve_sales_agent(sales_agent_id),
                ops_mode=ops_mode or TenantAccount.OPS_MODE_ONLINE,
            )
            if tenant and payment_channel and payment_transaction_ref:
                create_payment_submission(
                    tenant=tenant,
                    payment_kind="setup",
                    payment_channel=payment_channel,
                    transaction_ref=payment_transaction_ref,
                    submitted_by=user,
                )
        return user

    def update(self, instance, validated_data):
        validated_data.pop("payment_channel", None)
        validated_data.pop("payment_transaction_ref", None)
        validated_data.pop("sales_agent_id", None)
        validated_data.pop("ops_mode", None)
        password = validated_data.pop("password", None)
        profile_fields = {
            "clinic_name": validated_data.pop("clinic_name", None),
            "role": validated_data.pop("role", None),
            "logoUrl": validated_data.pop("logoUrl", None),
            "branch_name": validated_data.pop("branch_name", None),
            "clinic_tin": validated_data.pop("clinic_tin", None),
        }
        instance.username = validated_data.get("username", instance.username)
        if password:
            instance.set_password(password)
            password_changed = True
        else:
            password_changed = False
        is_active = validated_data.pop("is_active", None)
        instance.save()
        profile, _ = UserProfile.objects.get_or_create(user=instance)
        for attr, value in profile_fields.items():
            if value is not None:
                setattr(profile, attr, value)
        if password_changed:
            profile.password_changed_at = timezone.now()
        if is_active is not None:
            profile.is_active = is_active
            instance.is_active = is_active
            instance.save(update_fields=["is_active"])
        profile.save()
        return instance


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    def _sign_in(self, attrs):
        """Check the password against the role login and the people under it.

        Returns the tokens, or None when nobody has this username and password (the caller then lets the standard
        sign-in raise its usual error). Several people share a role's username; their own password tells them apart,
        and the matched person is carried in the token so every later action can name who did it.
        """
        username = (attrs.get(self.username_field) or "").strip()
        password = attrs.get("password") or ""
        user = User.objects.filter(username__iexact=username).select_related("profile").first()
        if user is None or not user.is_active:
            return None
        profile = getattr(user, "profile", None)
        person = None
        if profile is not None and (profile.role or "").lower() != "manager":
            matched, person = identify(profile, password)
            if matched and person is None and not profile.persons.exists():
                person = ensure_default_person(profile)
        else:
            matched = user.check_password(password)
        if not matched:
            return None
        self.user = user
        self.person = person
        refresh = self.get_token(user)
        if person is not None:
            refresh["person_id"] = person.id
            refresh["person_name"] = person.full_name
        return {"refresh": str(refresh), "access": str(refresh.access_token)}

    def validate(self, attrs):
        self.person = None
        data = self._sign_in(attrs)
        if data is None:
            data = super().validate(attrs)  # raises the standard "no active account" error
        profile = getattr(self.user, "profile", None)
        if profile and not profile.is_active:
            raise AuthenticationFailed("This staff account is deactivated.")
        tin = getattr(profile, "clinic_tin", "") if profile else ""
        role = getattr(profile, "role", "") if profile else ""
        tenant = resolve_tenant_account(tin) if tin else None
        if tenant is None and tin:
            tenant = ensure_tenant_account(
                clinic_tin=tin,
                clinic_name=getattr(profile, "clinic_name", ""),
                logo_url=getattr(profile, "logoUrl", ""),
                branch_name=getattr(profile, "branch_name", "") or "Main",
            )
        if tenant is None:
            raise AuthenticationFailed("Clinic tenant was not found.")
        decision = resolve_login_access(tenant, role=role)
        # An offline clinic that has not synced in time: staff wait for the manager, who gets Sync + Billing only.
        from clinic.sync_gate import apply_gate, gate_for_profile

        gate = gate_for_profile(profile, tenant)
        decision = apply_gate(decision, gate, role)
        if decision.access_mode == "denied":
            raise AuthenticationFailed(decision.detail or "Access denied.")
        data["user"] = UserSerializer(self.user).data
        data["person"] = (
            {"id": self.person.id, "name": self.person.full_name, "is_default": self.person.is_default}
            if self.person is not None
            else None
        )
        audit.record(
            self.context.get("request"),
            action="login",
            entity="session",
            summary="Signed in",
            user=self.user,
            person=self.person,
        )
        data["sync_gate"] = gate
        data["access_mode"] = decision.access_mode
        data["payment_kind"] = decision.payment_kind
        data["period_status"] = decision.period_status
        data["billing"] = billing_snapshot(tenant)
        return data
