from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from api.models import UserProfile
from clinic.models import ClinicBranch
from tenants.models import TenantAccount
from tenants.services import ensure_clinic_branch

ILLUST_BRANCH_NAME = "Bole"
ILLUST_BRANCH_ADDRESS = "Addis Ababa Bolle infront of skylight hotel"


class Command(BaseCommand):
    help = (
        "Set the illustration tenant main branch to Bole "
        f"({ILLUST_BRANCH_ADDRESS})."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tin",
            default="",
            help="Illustration clinic TIN (default: first is_illustration tenant).",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        tin = (options["tin"] or "").strip()
        if tin:
            tenant = TenantAccount.objects.filter(clinic_tin=tin).first()
        else:
            tenant = (
                TenantAccount.objects.filter(is_illustration=True)
                .order_by("clinic_tin")
                .first()
            )
        if not tenant:
            raise CommandError("Illustration tenant not found.")

        tenant.branch_name = ILLUST_BRANCH_NAME
        tenant.save(update_fields=["branch_name", "updated_at"])

        # Collapse legacy "Main" / other names into the Bole main site.
        others = ClinicBranch.objects.filter(clinic_tin=tenant.clinic_tin).exclude(
            name__iexact=ILLUST_BRANCH_NAME
        )
        removed = others.count()
        others.delete()

        ensure_clinic_branch(
            clinic_tin=tenant.clinic_tin,
            name=ILLUST_BRANCH_NAME,
            address=ILLUST_BRANCH_ADDRESS,
            is_main=True,
        )
        UserProfile.objects.filter(clinic_tin=tenant.clinic_tin).update(
            branch_name=ILLUST_BRANCH_NAME
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Illustration tenant {tenant.clinic_name} ({tenant.clinic_tin}) "
                f"main branch '{ILLUST_BRANCH_NAME}' "
                f"({ILLUST_BRANCH_ADDRESS}). Removed {removed} other branch row(s)."
            )
        )
