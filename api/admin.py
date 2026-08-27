from django.contrib import admin

from .models import UserProfile


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "clinic_name", "role", "updated_at")
    search_fields = ("user__username", "clinic_name", "role")
