from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("clinic", "0004_medicine_catalog_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="medicine",
            name="category",
            field=models.CharField(
                choices=[
                    ("antibiotic", "Antibiotic"),
                    ("analgesic", "Analgesic"),
                    ("antipyretic", "Antipyretic"),
                    ("antihypertensive", "Antihypertensive"),
                    ("vitamin", "Vitamin / supplement"),
                    ("antacid", "Antacid"),
                    ("topical", "Topical"),
                    ("infusion", "Infusion / injectable"),
                    ("other", "Other"),
                ],
                default="other",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="medicine",
            name="batch_number",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="medicine",
            name="expiry_date",
            field=models.DateField(blank=True, null=True),
        ),
    ]
