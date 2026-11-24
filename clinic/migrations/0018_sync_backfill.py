"""Step 2 of 3: give every existing row a sync_uuid and an opening stock movement per medicine."""
import uuid

from django.db import migrations

SYNCED = [
    "Department", "ClinicBranch", "Patient", "Encounter", "BillableService", "BillableItem",
    "PaymentTransaction", "RefundTransaction", "ClinicalOrder", "DoctorChart", "NurseNote",
    "Appointment", "Supplier", "Medicine", "MedicineDamage", "EquipmentTicket", "Referral",
    "LabTestParameter", "OrderResultValue", "RadiologyReport",
]


def backfill(apps, schema_editor):
    for name in SYNCED:
        model = apps.get_model("clinic", name)
        pending = list(model.objects.filter(sync_uuid__isnull=True).only("pk"))
        for start in range(0, len(pending), 500):
            chunk = pending[start : start + 500]
            for row in chunk:
                row.sync_uuid = uuid.uuid4()
            model.objects.bulk_update(chunk, ["sync_uuid"])

    # Opening balance: the ledger must add up to the stock that is already on the shelf.
    Medicine = apps.get_model("clinic", "Medicine")
    StockMovement = apps.get_model("clinic", "StockMovement")
    already = set(StockMovement.objects.values_list("medicine_id", flat=True))
    rows = []
    for m in Medicine.objects.all().iterator():
        if m.pk in already:
            continue
        size = max(1, int(m.pack_size or 1))
        units = m.on_hand * size + m.loose_stock if size > 1 else m.on_hand
        if units == 0:
            continue
        rows.append(
            StockMovement(
                clinic_tin=m.clinic_tin,
                branch_name=m.branch_name,
                medicine=m,
                kind="opening",
                delta=units,
                pack_size=size,
                reference="migration",
            )
        )
    StockMovement.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):
    dependencies = [("clinic", "0017_sync_fields")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
