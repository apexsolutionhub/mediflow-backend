"""Step 1 of 3: sync columns (nullable uuid, soft-delete, updated_at) and the ledger / sync tables.

Additive only, so the older deployed backend keeps working against the same database.
`sync_uuid` is added plain here, filled in by 0018 and made unique by 0019.
"""
import uuid

import django.db.models.deletion
import django.db.models.functions.datetime
from django.conf import settings
from django.db import migrations, models

SYNCED = [
    "department", "clinicbranch", "patient", "encounter", "billableservice", "billableitem",
    "paymenttransaction", "refundtransaction", "clinicalorder", "doctorchart", "nursenote",
    "appointment", "supplier", "medicine", "medicinedamage", "equipmentticket", "referral",
    "labtestparameter", "orderresultvalue", "radiologyreport",
]
# These already had an `updated_at` of their own.
HAS_UPDATED_AT = {"clinicalorder", "doctorchart", "supplier", "medicinedamage", "radiologyreport"}


def _ops():
    ops = []
    for model in SYNCED:
        ops.append(
            migrations.AddField(
                model_name=model,
                name="sync_uuid",
                field=models.UUIDField(editable=False, null=True),
            )
        )
        ops.append(
            migrations.AddField(
                model_name=model,
                name="deleted_at",
                field=models.DateTimeField(blank=True, null=True),
            )
        )
        if model not in HAS_UPDATED_AT:
            ops.append(
                migrations.AddField(
                    model_name=model,
                    name="updated_at",
                    field=models.DateTimeField(
                        auto_now=True, db_default=django.db.models.functions.datetime.Now()
                    ),
                )
            )
    return ops


class Migration(migrations.Migration):
    dependencies = [
        ("clinic", "0016_encounter_arrival_no_referred"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = _ops() + [
        migrations.CreateModel(
            name="SyncState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("clinic_tin", models.CharField(max_length=50)),
                ("branch_name", models.CharField(blank=True, default="", max_length=120)),
                ("seeded_at", models.DateTimeField(blank=True, null=True)),
                ("last_successful_sync_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_token", models.CharField(blank=True, default="", max_length=128)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddConstraint(
            model_name="syncstate",
            constraint=models.UniqueConstraint(fields=("clinic_tin", "branch_name"), name="uniq_sync_state_scope"),
        ),
        migrations.CreateModel(
            name="SyncRun",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sync_uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("clinic_tin", models.CharField(db_index=True, max_length=50)),
                ("branch_name", models.CharField(blank=True, default="", max_length=120)),
                ("kind", models.CharField(choices=[("sync", "Sync"), ("seed", "Initial seed")], default="sync", max_length=8)),
                (
                    "push_status",
                    models.CharField(
                        choices=[("idle", "Idle"), ("running", "Running"), ("ok", "OK"), ("failed", "Failed"), ("skipped", "Skipped")],
                        default="idle",
                        max_length=10,
                    ),
                ),
                (
                    "pull_status",
                    models.CharField(
                        choices=[("idle", "Idle"), ("running", "Running"), ("ok", "OK"), ("failed", "Failed"), ("skipped", "Skipped")],
                        default="idle",
                        max_length=10,
                    ),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "started_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.CreateModel(
            name="StockMovement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("sync_uuid", models.UUIDField(default=uuid.uuid4, editable=False, null=True, unique=True)),
                ("clinic_tin", models.CharField(db_index=True, max_length=50)),
                ("branch_name", models.CharField(blank=True, db_index=True, default="", max_length=120)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("opening", "Opening balance"),
                            ("receive", "Received"),
                            ("dispense", "Dispensed"),
                            ("damage", "Damaged"),
                            ("adjust", "Adjustment"),
                        ],
                        default="adjust",
                        max_length=16,
                    ),
                ),
                ("delta", models.IntegerField()),
                ("pack_size", models.PositiveIntegerField(default=1)),
                ("reference", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "medicine",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="movements",
                        to="clinic.medicine",
                    ),
                ),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddIndex(
            model_name="stockmovement",
            index=models.Index(fields=["medicine", "created_at"], name="clinic_stoc_medicin_c8e4bd_idx"),
        ),
    ]
