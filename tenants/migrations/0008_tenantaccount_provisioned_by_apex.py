from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tenants", "0007_tenantfeedbackmessage_tenantfeedbackthread"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="""
                        SET @exists = (
                            SELECT COUNT(*)
                            FROM information_schema.COLUMNS
                            WHERE TABLE_SCHEMA = DATABASE()
                              AND TABLE_NAME = 'tenants_tenantaccount'
                              AND COLUMN_NAME = 'provisioned_by_apex'
                        );
                        SET @sql = IF(
                            @exists = 0,
                            'ALTER TABLE tenants_tenantaccount ADD COLUMN provisioned_by_apex tinyint(1) NOT NULL DEFAULT 0',
                            'SELECT 1'
                        );
                        PREPARE stmt FROM @sql;
                        EXECUTE stmt;
                        DEALLOCATE PREPARE stmt;
                    """,
                    reverse_sql=migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="tenantaccount",
                    name="provisioned_by_apex",
                    field=models.BooleanField(default=False),
                ),
            ],
        ),
    ]
