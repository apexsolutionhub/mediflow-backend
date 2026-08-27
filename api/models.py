from django.conf import settings
from django.db import models


class UserProfile(models.Model):
    ROLE_CHOICES = (
        ("manager", "Manager"),
        ("reception", "Reception"),
        ("doctor", "Doctor"),
        ("nurse", "Nurse"),
        ("lab", "Lab"),
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} ({self.role})"
