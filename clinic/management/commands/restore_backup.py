"""Restore a clinic server's database from a backup (after a disk failure, or on a new PC).

  python manage.py restore_backup --yes                 # the newest backup in the backup folder
  python manage.py restore_backup --file D:/backups/mediflow-20261124-081500-scheduled.sql.gz --yes
  python manage.py restore_backup --yes --tin 1234567890 --branch Main

This REPLACES everything in the database. Afterwards the server needs a sync before staff can
sign in, so the cloud and the restored copy are brought back together.
"""
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from clinic import backup


class Command(BaseCommand):
    help = "Replace the clinic database with a backup, then require a sync."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="A backup file (default: the newest in the backup folder).")
        parser.add_argument("--yes", action="store_true", help="Confirm that the current database may be replaced.")
        parser.add_argument("--tin", default="", help="Clinic TIN (default: NODE_CLINIC_TIN).")
        parser.add_argument("--branch", default="", help="Branch (default: NODE_BRANCH).")

    def handle(self, *args, **opts):
        if not opts["yes"]:
            raise CommandError("This replaces the whole database. Run it again with --yes to confirm.")
        if opts["file"]:
            path = Path(opts["file"])
        else:
            found = backup.list_backups()
            if not found:
                raise CommandError(f"No backups found in {backup.backup_dir()}.")
            path = found[0]
        if not path.is_file():
            raise CommandError(f"File not found: {path}")
        tin = opts["tin"] or getattr(settings, "NODE_CLINIC_TIN", "")
        branch = opts["branch"] or getattr(settings, "NODE_BRANCH", "Main")
        try:
            backup.restore_backup(path, (tin, branch) if tin else None)
        except backup.BackupError as exc:
            raise CommandError(str(exc))
        self.stdout.write(self.style.SUCCESS(f"Restored from {path.name}."))
        if tin:
            self.stdout.write("The manager must now run a sync before staff can sign in.")
        else:
            self.stdout.write("No clinic TIN is set, so run: manage.py flag_node_restore --tin ... --branch ...")
