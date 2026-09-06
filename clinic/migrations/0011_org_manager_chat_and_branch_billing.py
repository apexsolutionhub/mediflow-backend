from datetime import timedelta

from django.db import migrations, models
import django.db.models.deletion
from django.conf import settings
from django.utils import timezone


def provision_branch_billing_accounts(apps, schema_editor):
    ClinicBranch = apps.get_model("clinic", "ClinicBranch")
    TenantAccount = apps.get_model("tenants", "TenantAccount")

    for branch in ClinicBranch.objects.all():
        op_tin = (branch.branch_tin or branch.clinic_tin or "").strip()
        org_tin = (branch.clinic_tin or "").strip()
        if not op_tin:
            continue
        if TenantAccount.objects.filter(clinic_tin=op_tin).exists():
            continue
        parent = TenantAccount.objects.filter(clinic_tin=org_tin).first()
        name = (parent.clinic_name if parent else "") or ""
        branch_label = (branch.name or "").strip()
        display = f"{name} — {branch_label}".strip(" —") if name else branch_label or op_tin
        TenantAccount.objects.create(
            clinic_tin=op_tin,
            clinic_name=display,
            branch_name=branch_label or "Main",
            logo_url=(parent.logo_url if parent else "") or "",
            account_status=TenantAccount.STATUS_ACTIVE,
            setup_fee_etb=getattr(parent, "setup_fee_etb", 15000) if parent else 15000,
            quarterly_fee_etb=getattr(parent, "quarterly_fee_etb", 5000) if parent else 5000,
            yearly_fee_etb=getattr(parent, "yearly_fee_etb", 0) if parent else 0,
            setup_fee_approved=False,
            subscription_payment_approved=False,
            provisioned_by_apex=bool(getattr(parent, "provisioned_by_apex", False)) if parent else False,
            is_illustration=bool(getattr(parent, "is_illustration", False)) if parent else False,
            fees_manually_set=bool(getattr(parent, "fees_manually_set", False)) if parent else False,
            modules=list(getattr(parent, "modules", []) or []) if parent else [],
            ops_mode=getattr(parent, "ops_mode", "online") if parent else "online",
            free_trial_ends_at=timezone.now() + timedelta(days=7),
            sales_agent_id=getattr(parent, "sales_agent_id", None) if parent else None,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0010_clinicbranch_branch_tin"),
        ("tenants", "0008_tenantaccount_provisioned_by_apex"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OrgManagerThread",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("org_tin", models.CharField(db_index=True, max_length=50)),
                ("tin_a", models.CharField(db_index=True, max_length=50)),
                ("tin_b", models.CharField(db_index=True, max_length=50)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "ordering": ["-updated_at"],
                "unique_together": {("org_tin", "tin_a", "tin_b")},
            },
        ),
        migrations.CreateModel(
            name="OrgManagerMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sender_tin", models.CharField(db_index=True, max_length=50)),
                ("body", models.TextField(blank=True, default="")),
                ("image_url", models.URLField(blank=True, default="")),
                ("read_by_recipient", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "sender",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="org_manager_messages",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="messages",
                        to="clinic.orgmanagerthread",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
            },
        ),
        migrations.RunPython(provision_branch_billing_accounts, migrations.RunPython.noop),
    ]
