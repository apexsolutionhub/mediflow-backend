from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    ROLE_CHOICES = (
        ("manager", "Manager"),
        ("reception", "Reception"),
        ("doctor", "Doctor"),
        ("nurse", "Nurse"),
        ("lab", "Lab"),
        ("radiology", "Radiology"),
        ("pharmacist", "Pharmacist"),
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="profile",
    )
    clinic_name = models.CharField(max_length=255, blank=True)
    clinic_tin = models.CharField(max_length=50, blank=True, db_index=True)
    role = models.CharField(max_length=32, choices=ROLE_CHOICES, default="manager")
    logoUrl = models.URLField(blank=True)
    branch_name = models.CharField(max_length=255, default="Main")
    is_active = models.BooleanField(default=True)
    # When the password last changed. A clinic server and the cloud compare it to decide whose password wins.
    password_changed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"


class StaffPerson(models.Model):
    """One person who works under a role's shared login.

    Everyone in a role signs in with the same username and tells themselves apart by password. The default person
    has no password of their own here: the role account's password is theirs (so promoting someone else to default
    is just moving their password to the account).
    """

    profile = models.ForeignKey(UserProfile, on_delete=models.CASCADE, related_name="persons")
    full_name = models.CharField(max_length=255)
    password_hash = models.CharField(max_length=255, blank=True, default="")
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "full_name", "id"]

    def __str__(self):
        return f"{self.full_name} ({self.profile.role})"


class AuditEvent(models.Model):
    """Who did what. Written by the server for every successful change, never edited."""

    clinic_tin = models.CharField(max_length=50, blank=True, db_index=True)
    branch_name = models.CharField(max_length=255, blank=True, default="")
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    actor_person = models.ForeignKey(StaffPerson, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Names are copied in so the log still reads correctly after a person or account is removed.
    actor_name = models.CharField(max_length=255, blank=True, default="")
    actor_username = models.CharField(max_length=150, blank=True, default="")
    actor_role = models.CharField(max_length=32, blank=True, default="")
    action = models.CharField(max_length=40, db_index=True)
    entity = models.CharField(max_length=80, blank=True, default="")
    entity_id = models.CharField(max_length=64, blank=True, default="")
    summary = models.CharField(max_length=500, blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["clinic_tin", "branch_name", "-created_at"])]

    def __str__(self):
        return f"{self.actor_name or self.actor_username}: {self.summary or self.action}"
