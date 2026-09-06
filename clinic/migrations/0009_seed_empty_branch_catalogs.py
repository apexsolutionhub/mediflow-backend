from django.db import migrations


def seed_empty_branch_catalogs(apps, schema_editor):
    ClinicBranch = apps.get_model("clinic", "ClinicBranch")
    Department = apps.get_model("clinic", "Department")
    BillableService = apps.get_model("clinic", "BillableService")

    defaults = [
        ("CONSULT", "Consultation", "Consultation", "consultation", 300, True),
        ("LAB-CBC", "Complete Blood Count", "Laboratory", "lab", 250, False),
        ("RAD-XRAY", "Chest X-ray", "Radiology", "radiology", 400, False),
        ("RX-DISP", "Pharmacy dispensing", "Pharmacy", "pharmacy", 50, False),
    ]

    for branch in ClinicBranch.objects.all():
        tin = (branch.clinic_tin or "").strip()
        name = (branch.name or "").strip() or "Main"
        if not tin:
            continue
        if Department.objects.filter(clinic_tin=tin, branch_name__iexact=name).exists():
            continue
        Department.objects.create(
            clinic_tin=tin, name="General", branch_name=name, is_active=True
        )
        for code, svc_name, dept, service_type, price, auto_add in defaults:
            BillableService.objects.get_or_create(
                clinic_tin=tin,
                code=code,
                branch_name=name,
                defaults={
                    "name": svc_name,
                    "department": dept,
                    "service_type": service_type,
                    "unit_price": price,
                    "auto_add_on_registration": auto_add,
                    "is_active": True,
                    "default_quantity": 1,
                    "requires_payment_before_work": True,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0008_branch_scoped_catalog"),
    ]

    operations = [
        migrations.RunPython(seed_empty_branch_catalogs, migrations.RunPython.noop),
    ]
