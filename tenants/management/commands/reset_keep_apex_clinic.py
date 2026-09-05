from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import models, transaction

from api.models import UserProfile
from clinic.models import (
    Appointment,
    BillableService,
    ClinicBranch,
    Department,
    Encounter,
    EquipmentTicket,
    Medicine,
    Patient,
)
from tenants.models import (
    TenantAccount,
    TenantFeedbackMessage,
    TenantFeedbackThread,
    TenantOpsModeChangeRequest,
    TenantPaymentSubmission,
)

User = get_user_model()


class Command(BaseCommand):
    help = (
        "Delete all tenants except the illustration Apex clinic, remove all clinical "
        "and transactional data (including inside Apex), and keep Apex role logins."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tin",
            default="",
            help="Apex clinic TIN to preserve (default: first is_illustration tenant).",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Required. Confirms you want to permanently delete data.",
        )

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Pass --confirm to run this destructive reset.")

        apex_tin = (options["tin"] or "").strip()
        if apex_tin:
            apex = TenantAccount.objects.filter(clinic_tin=apex_tin).first()
        else:
            apex = TenantAccount.objects.filter(is_illustration=True).order_by("clinic_tin").first()
            apex_tin = apex.clinic_tin if apex else ""

        if not apex:
            raise CommandError("Apex illustration tenant not found.")

        keep_user_ids = list(
            UserProfile.objects.filter(clinic_tin=apex_tin).values_list("user_id", flat=True)
        )
        staff_user_ids = list(
            User.objects.filter(is_active=True)
            .filter(models.Q(is_staff=True) | models.Q(is_superuser=True))
            .values_list("id", flat=True)
        )
        keep_user_ids = list(set(keep_user_ids) | set(staff_user_ids))
        if not keep_user_ids:
            raise CommandError(f"No staff users found for clinic TIN {apex_tin}.")

        self.stdout.write(
            self.style.WARNING(
                f"Resetting database — keeping tenant {apex.clinic_name} ({apex_tin}) "
                f"and {len(keep_user_ids)} staff account(s)."
            )
        )

        with transaction.atomic():
            counts = self._wipe_clinical_data()
            counts.update(self._wipe_tenant_transactions(apex_tin))
            counts.update(self._wipe_feedback(apex_tin))
            deleted_tenants, deleted_users = self._delete_other_tenants_and_users(
                apex_tin, keep_user_ids
            )
            self._reset_apex_tenant(apex)

        self.stdout.write(self.style.SUCCESS("Database reset complete."))
        self.stdout.write(f"  Kept tenant: {apex.clinic_name} ({apex_tin})")
        self.stdout.write(f"  Kept users: {len(keep_user_ids)}")
        self.stdout.write(f"  Deleted tenants: {deleted_tenants}")
        self.stdout.write(f"  Deleted users: {deleted_users}")
        for label, count in sorted(counts.items()):
            if count:
                self.stdout.write(f"  Removed {label}: {count}")

    def _wipe_clinical_data(self) -> dict[str, int]:
        return {
            "encounters (+ related charts/orders/payments)": Encounter.objects.all().delete()[0],
            "appointments": Appointment.objects.all().delete()[0],
            "equipment tickets": EquipmentTicket.objects.all().delete()[0],
            "medicines": Medicine.objects.all().delete()[0],
            "billable services": BillableService.objects.all().delete()[0],
            "patients": Patient.objects.all().delete()[0],
            "departments": Department.objects.all().delete()[0],
            "clinic branches": ClinicBranch.objects.all().delete()[0],
        }

    def _wipe_tenant_transactions(self, apex_tin: str) -> dict[str, int]:
        return {
            "payment submissions": TenantPaymentSubmission.objects.all().delete()[0],
            "ops mode requests": TenantOpsModeChangeRequest.objects.all().delete()[0],
        }

    def _wipe_feedback(self, apex_tin: str) -> dict[str, int]:
        message_count = TenantFeedbackMessage.objects.all().delete()[0]
        thread_count = TenantFeedbackThread.objects.all().delete()[0]
        return {
            "feedback messages": message_count,
            "feedback threads": thread_count,
        }

    def _delete_other_tenants_and_users(
        self, apex_tin: str, keep_user_ids: list[int]
    ) -> tuple[int, int]:
        deleted_users = User.objects.exclude(id__in=keep_user_ids).delete()[0]
        deleted_tenants = TenantAccount.objects.exclude(clinic_tin=apex_tin).delete()[0]
        return deleted_tenants, deleted_users

    def _reset_apex_tenant(self, tenant: TenantAccount) -> None:
        branch_name = "Bole"
        branch_address = "Addis Ababa Bolle infront of skylight hotel"
        tenant.account_status = TenantAccount.STATUS_ACTIVE
        tenant.is_illustration = True
        tenant.setup_fee_approved = True
        tenant.subscription_payment_approved = True
        tenant.billing_hold = False
        tenant.ops_mode = TenantAccount.OPS_MODE_ONLINE
        tenant.payment_channel = ""
        tenant.payment_transaction_ref = ""
        tenant.billing_notes = ""
        tenant.status_reason = ""
        tenant.status_changed_at = None
        tenant.subscription_paid_until = None
        tenant.free_trial_ends_at = None
        tenant.paid_quarters_count = 0
        tenant.billing_started_at = None
        tenant.sales_agent = None
        tenant.branch_name = branch_name
        tenant.save()

        ClinicBranch.objects.filter(clinic_tin=tenant.clinic_tin).delete()
        ClinicBranch.objects.create(
            clinic_tin=tenant.clinic_tin,
            name=branch_name,
            address=branch_address,
            is_main=True,
            is_active=True,
        )
        UserProfile.objects.filter(clinic_tin=tenant.clinic_tin).update(branch_name=branch_name)
