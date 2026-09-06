from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("clinic", "0006_clinicbranch_order_fulfillment"),
    ]

    operations = [
        migrations.AddField(
            model_name="referral",
            name="destination_kind",
            field=models.CharField(
                choices=[("internal", "Same organization"), ("external", "Outside organization")],
                db_index=True,
                default="internal",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="referral",
            name="external_institution",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="referral",
            name="approval_status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending manager approval"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="referral",
            name="approved_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="referral",
            name="approved_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="approved_referrals",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
