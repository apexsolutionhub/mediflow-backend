"""Check that backups are readable and complete, without touching the database.

  python manage.py verify_backup                  # every backup in the backup folder (and the second location)
  python manage.py verify_backup --file D:/x.sql.gz

Run it now and then (and before relying on a backup): a backup nobody has checked is only a hope.
Exits with an error if the newest backup is damaged or there are none.
"""
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from clinic import backup


class Command(BaseCommand):
    help = "Verify backup files (readable, complete) without restoring them."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Check one file instead of the backup folders.")

    def _check(self, path: Path) -> bool:
        try:
            backup.verify_dump(path)
        except backup.BackupError as exc:
            self.stdout.write(self.style.ERROR(f"  BAD  {path.name}: {exc}"))
            return False
        self.stdout.write(f"  ok   {path.name} ({path.stat().st_size // 1024} KB)")
        return True

    def handle(self, *args, **opts):
        if opts["file"]:
            path = Path(opts["file"])
            if not path.is_file():
                raise CommandError(f"File not found: {path}")
            if not self._check(path):
                raise CommandError("This backup is damaged. Do not restore from it.")
            return

        newest_ok = None
        for label, folder in (("Backup folder", backup.backup_dir()), ("Second location", backup.copy_dir())):
            if folder is None:
                continue
            files = backup.list_backups(folder)
            self.stdout.write(f"{label}: {folder} ({len(files)} backup(s))")
            for index, path in enumerate(files):
                ok = self._check(path)
                if index == 0 and label == "Backup folder":
                    newest_ok = ok
        if newest_ok is None:
            raise CommandError("There are no backups. Run a backup first.")
        if not newest_ok:
            raise CommandError("The newest backup is damaged. Make a new backup now.")
        self.stdout.write(self.style.SUCCESS("The newest backup checks out."))
