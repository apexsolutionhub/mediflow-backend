# Generated manually for billable service catalog fields

from django.db import migrations, models


def backfill_service_types(apps, schema_editor):
    BillableService = apps.get_model("clinic", "BillableService")
    code_map = {
        "CONSULT": ("consultation", True),
        "LAB-CBC": ("lab", False),
        "RAD-XRAY": ("radiology", False),
        "RX-DISP": ("pharmacy", False),
    }
    dept_map = {
        "consultation": "consultation",
        "laboratory": "lab",
        "lab": "lab",
        "radiology": "radiology",
        "pharmacy": "pharmacy",
        "nursing": "nursing",
        "procedure": "procedure",
    }
    for svc in BillableService.objects.all():
        if svc.code in code_map:
            svc.service_type, auto_add = code_map[svc.code]
            if auto_add:
                svc.auto_add_on_registration = True
        else:
            dept_key = (svc.department or "").strip().lower()
            svc.service_type = dept_map.get(dept_key, "other")
        svc.save(update_fields=["service_type", "auto_add_on_registration"])


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0002_billable_service_department_freetext"),
    ]

    operations = [
        migrations.AddField(
            model_name="billableservice",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="service_type",
            field=models.CharField(
                choices=[
                    ("consultation", "Consultation"),
                    ("lab", "Lab"),
                    ("radiology", "Radiology"),
                    ("pharmacy", "Pharmacy"),
                    ("procedure", "Procedure"),
                    ("nursing", "Nursing"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="default_quantity",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="auto_add_on_registration",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="requires_payment_before_work",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="billableservice",
            name="internal_notes",
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(backfill_service_types, migrations.RunPython.noop),
    ]
