"""Mark a clinic node as restored from a backup (or rebuilt on a new PC).

  python manage.py flag_node_restore --tin 1234567890 --branch Main

Until the manager completes a sync, staff cannot sign in and the manager can only open Sync and
Billing. The restore kit runs this right after loading the latest dump.
"""
from django.core.management.base import BaseCommand

from clinic.models import SyncState


class Command(BaseCommand):
    help = "Require a sync before staff can sign in again (after a restore)."

    def add_arguments(self, parser):
        parser.add_argument("--tin", required=True)
        parser.add_argument("--branch", default="Main")

    def handle(self, *args, **opts):
        state, _ = SyncState.objects.get_or_create(clinic_tin=opts["tin"], branch_name=opts["branch"])
        state.restore_pending = True
        state.save(update_fields=["restore_pending", "updated_at"])
        self.stdout.write(self.style.SUCCESS(f"{opts['tin']} / {opts['branch']}: sync is now required."))
