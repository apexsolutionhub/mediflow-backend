"""Apply an approved ops-mode change for a clinic after its seed / final sync succeeded.

Until the sync endpoints exist (phase 1) Apex support runs this by hand:
  python manage.py complete_ops_mode_switch --tin 1234567890
"""
from django.core.management.base import BaseCommand, CommandError

from tenants.ops_mode_views import active_ops_mode_block, awaiting_step, complete_ops_mode_switch
from tenants.services import resolve_tenant_account


class Command(BaseCommand):
    help = "Apply the approved ops-mode change for a clinic (after its seed or final sync)."

    def add_arguments(self, parser):
        parser.add_argument("--tin", required=True)

    def handle(self, *args, **opts):
        tenant = resolve_tenant_account(opts["tin"])
        if not tenant:
            raise CommandError(f"No clinic with TIN {opts['tin']}.")
        waiting = active_ops_mode_block(tenant)
        if not waiting:
            self.stdout.write("Nothing waiting: no approved, unapplied change.")
            return
        step = awaiting_step(waiting)
        applied = complete_ops_mode_switch(tenant)
        self.stdout.write(
            self.style.SUCCESS(
                f"{tenant.clinic_tin}: {applied.current_ops_mode} -> {applied.requested_ops_mode} applied (after {step})."
            )
        )
