import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

User = get_user_model()


class Command(BaseCommand):
    help = "Create or reset the MediFlow Apex console superuser (mediflow_admin login)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default=os.environ.get("APEX_CONSOLE_USERNAME", "apexMediFlow"),
        )
        parser.add_argument(
            "--password",
            default=os.environ.get("APEX_CONSOLE_PASSWORD", ""),
            help="Required unless APEX_CONSOLE_PASSWORD is set in the environment.",
        )

    def handle(self, *args, **options):
        username = (options["username"] or "").strip()
        password = options["password"] or ""
        if not username:
            raise SystemExit("Username is required.")
        if not password:
            raise SystemExit(
                "Pass --password or set APEX_CONSOLE_PASSWORD in the environment."
            )

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "is_staff": True,
                "is_superuser": True,
                "is_active": True,
            },
        )
        user.is_staff = True
        user.is_superuser = True
        user.is_active = True
        user.set_password(password)
        user.save()

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{verb} Apex console user: {username}"))
