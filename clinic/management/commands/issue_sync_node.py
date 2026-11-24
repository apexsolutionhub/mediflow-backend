"""Issue the credential a clinic node uses to sync a branch with the cloud.

  python manage.py issue_sync_node --tin 1234567890 --branch Main --label "Reception PC"

The token is shown once. Put it in the NODE_TOKEN setting of the clinic server; only its hash is stored.
"""
from django.core.management.base import BaseCommand, CommandError

from clinic.sync_cloud import issue_node
from tenants.services import resolve_tenant_account


class Command(BaseCommand):
    help = "Issue a sync token for one clinic branch node."

    def add_arguments(self, parser):
        parser.add_argument("--tin", required=True)
        parser.add_argument("--branch", default="Main")
        parser.add_argument("--label", default="")

    def handle(self, *args, **opts):
        if not resolve_tenant_account(opts["tin"]):
            raise CommandError(f"No clinic with TIN {opts['tin']}.")
        node, token = issue_node(opts["tin"], opts["branch"], opts["label"])
        self.stdout.write(self.style.SUCCESS(f"Node {node.pk} issued for {node.clinic_tin} / {node.branch_name}."))
        self.stdout.write(f"NODE_TOKEN={token}")
        self.stdout.write("Copy it now; it cannot be shown again.")
