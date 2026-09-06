from django.db import migrations, models
from django.db.models import Q


def backfill_branch_names(apps, schema_editor):
    TenantAccount = apps.get_model("tenants", "TenantAccount")
    ClinicBranch = apps.get_model("clinic", "ClinicBranch")
    models_to_fix = [
        apps.get_model("clinic", "Department"),
        apps.get_model("clinic", "BillableService"),
        apps.get_model("clinic", "Medicine"),
        apps.get_model("clinic", "Patient"),
        apps.get_model("clinic", "Encounter"),
        apps.get_model("clinic", "Appointment"),
        apps.get_model("clinic", "EquipmentTicket"),
    ]

    tin_to_branch = {}
    for tenant in TenantAccount.objects.all():
        tin = (tenant.clinic_tin or "").strip()
        if not tin:
            continue
        name = (getattr(tenant, "branch_name", None) or "").strip()
        if not name:
            main = (
                ClinicBranch.objects.filter(clinic_tin=tin, is_main=True)
                .order_by("id")
                .first()
            )
            if main:
                name = (main.name or "").strip()
            else:
                any_branch = ClinicBranch.objects.filter(clinic_tin=tin).order_by("id").first()
                name = (any_branch.name if any_branch else "") or "Main"
        tin_to_branch[tin] = name

    for Model in models_to_fix:
        for row in Model.objects.filter(Q(branch_name="") | Q(branch_name__isnull=True)):
            tin = (row.clinic_tin or "").strip()
            row.branch_name = tin_to_branch.get(tin) or "Main"
            row.save(update_fields=["branch_name"])


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0007_referral_outside_approval"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="encounter",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="equipmentticket",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="medicine",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="patient",
            name="branch_name",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AlterUniqueTogether(
            name="billableservice",
            unique_together={("clinic_tin", "code", "branch_name")},
        ),
        migrations.RunPython(backfill_branch_names, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name="patient",
            unique_together={("clinic_tin", "mrn", "branch_name")},
        ),
        migrations.AlterUniqueTogether(
            name="encounter",
            unique_together={("clinic_tin", "number", "branch_name")},
        ),
    ]
