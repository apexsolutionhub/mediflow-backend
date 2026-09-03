# Generated manually for radiology staff role

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("api", "0002_userprofile_branch_name_userprofile_clinic_tin_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="userprofile",
            name="role",
            field=models.CharField(
                choices=[
                    ("manager", "Manager"),
                    ("reception", "Reception"),
                    ("doctor", "Doctor"),
                    ("nurse", "Nurse"),
                    ("lab", "Lab"),
                    ("radiology", "Radiology"),
                    ("pharmacist", "Pharmacist"),
                ],
                default="manager",
                max_length=32,
            ),
        ),
    ]
