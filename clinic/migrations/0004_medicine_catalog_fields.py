# Generated for medicine catalog fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0003_billable_service_catalog_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="medicine",
            name="description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="medicine",
            name="unit_of_measure",
            field=models.CharField(
                choices=[
                    ("tablet", "Tablet"),
                    ("capsule", "Capsule"),
                    ("bottle", "Bottle"),
                    ("vial", "Vial"),
                    ("tube", "Tube"),
                    ("pack", "Pack"),
                    ("other", "Other"),
                ],
                default="tablet",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="medicine",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="medicine",
            name="internal_notes",
            field=models.TextField(blank=True),
        ),
    ]
