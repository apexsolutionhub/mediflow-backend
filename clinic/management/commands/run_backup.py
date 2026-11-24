"""Make a local backup of the clinic database.

  python manage.py run_backup                  # a manual backup
  python manage.py run_backup --reason scheduled

The scheduler on the clinic PC runs this every few hours. A failed backup exits with an error so
the scheduler (and anyone watching) can see it.
"""
from django.core.management.base import BaseCommand, CommandError

from clinic import backup


class Command(BaseCommand):
    help = "Dump the database to the backup folder (and the second location, if one is set)."

    def add_arguments(self, parser):
        parser.add_argument("--reason", default="manual", help="manual, scheduled or presync (part of the file name)")

    def handle(self, *args, **opts):
        result = backup.make_backup(opts["reason"])
        if not result.ok:
            raise CommandError(result.error or "The backup failed.")
        self.stdout.write(self.style.SUCCESS(f"Backup saved: {result.path} ({result.size // 1024} KB)"))
        if result.secondary_ok is False:
            self.stdout.write(self.style.WARNING(result.secondary_error))
        elif result.secondary_ok:
            self.stdout.write("Copied to the second location.")
        if result.removed:
            self.stdout.write(f"Removed {len(result.removed)} old backup(s).")
