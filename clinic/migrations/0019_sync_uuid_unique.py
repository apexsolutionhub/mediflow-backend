"""Step 3 of 3: sync_uuid becomes unique and gets its default."""
import uuid

from django.db import migrations, models

SYNCED = [
    "department", "clinicbranch", "patient", "encounter", "billableservice", "billableitem",
    "paymenttransaction", "refundtransaction", "clinicalorder", "doctorchart", "nursenote",
    "appointment", "supplier", "medicine", "medicinedamage", "equipmentticket", "referral",
    "labtestparameter", "orderresultvalue", "radiologyreport",
]


class Migration(migrations.Migration):
    dependencies = [("clinic", "0018_sync_backfill")]
    operations = [
        migrations.AlterField(
            model_name=model,
            name="sync_uuid",
            field=models.UUIDField(default=uuid.uuid4, editable=False, null=True, unique=True),
        )
        for model in SYNCED
    ]
