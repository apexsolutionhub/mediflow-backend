# Generated manually for ClinicBranch + order fulfillment + department branch_name

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0005_medicine_category_batch_expiry"),
    ]

    operations = [
        migrations.CreateModel(
            name="ClinicBranch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("clinic_tin", models.CharField(db_index=True, max_length=50)),
                ("name", models.CharField(max_length=120)),
                ("address", models.CharField(blank=True, default="", max_length=255)),
                ("is_main", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-is_main", "name"],
                "unique_together": {("clinic_tin", "name")},
            },
        ),
        migrations.AddField(
            model_name="department",
            name="branch_name",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AlterUniqueTogether(
            name="department",
            unique_together={("clinic_tin", "name", "branch_name")},
        ),
        migrations.AddField(
            model_name="clinicalorder",
            name="fulfillment",
            field=models.CharField(
                blank=True,
                choices=[("clinic_pharmacy", "Clinic pharmacy"), ("external_print", "External print")],
                default="clinic_pharmacy",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="clinicalorder",
            name="medicine",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="orders",
                to="clinic.medicine",
            ),
        ),
    ]
