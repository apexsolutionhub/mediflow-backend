from django.db import migrations, models
from django.db.models import Q


def backfill_branch_tin(apps, schema_editor):
    ClinicBranch = apps.get_model("clinic", "ClinicBranch")
    for row in ClinicBranch.objects.filter(Q(branch_tin="") | Q(branch_tin__isnull=True)):
        row.branch_tin = (row.clinic_tin or "").strip()
        row.save(update_fields=["branch_tin"])


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0009_seed_empty_branch_catalogs"),
    ]

    operations = [
        migrations.AddField(
            model_name="clinicbranch",
            name="branch_tin",
            field=models.CharField(blank=True, db_index=True, default="", max_length=50),
        ),
        migrations.RunPython(backfill_branch_tin, migrations.RunPython.noop),
    ]
