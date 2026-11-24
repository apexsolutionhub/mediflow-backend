"""Run a sync from the command line on a clinic server (no sign-in needed).

  python manage.py node_sync --seed     # first time: copy the clinic from the cloud onto this server
  python manage.py node_sync            # push local work to the cloud, then pull the cloud's changes

A brand-new server has no users yet, so the seed has to work without a login; the manager's
Sync page does the same job once staff exist.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from clinic.models import SyncRun
from clinic.sync_client import HttpTransport, run_sync
from clinic.sync_engine import Scope


class Command(BaseCommand):
    help = "Sync this clinic server with the cloud (push, then pull)."

    def add_arguments(self, parser):
        parser.add_argument("--seed", action="store_true", help="Initial seed: copy everything from the cloud.")

    def handle(self, *args, **opts):
        if settings.NODE_MODE != "clinic":
            raise CommandError("This is not a clinic server (NODE_MODE is not 'clinic').")
        missing = [
            name
            for name in ("CLOUD_API_URL", "NODE_TOKEN", "NODE_CLINIC_TIN")
            if not getattr(settings, name, "")
        ]
        if missing:
            raise CommandError(f"Set {', '.join(missing)} in the server settings first.")
        kind = SyncRun.KIND_SEED if opts["seed"] else SyncRun.KIND_SYNC
        run = run_sync(
            Scope(settings.NODE_CLINIC_TIN, settings.NODE_BRANCH),
            HttpTransport(settings.CLOUD_API_URL, settings.NODE_TOKEN),
            kind=kind,
        )
        self.stdout.write(f"push: {run.push_status}   pull: {run.pull_status}")
        if SyncRun.STEP_FAILED in (run.push_status, run.pull_status) or run.pull_status != SyncRun.STEP_OK:
            raise CommandError(run.error_message or "The sync did not finish.")
        self.stdout.write(self.style.SUCCESS("Seed complete." if opts["seed"] else "Sync complete."))
