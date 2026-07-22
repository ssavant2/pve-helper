import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [
        migrations.CreateModel(
            name="LogForwarderConfiguration",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("enabled", models.BooleanField(default=False)),
                ("host", models.CharField(blank=True, max_length=255)),
                ("port", models.PositiveIntegerField(default=6514)),
                (
                    "transport",
                    models.CharField(choices=[("tls", "TCP with TLS"), ("tcp", "TCP")], default="tls", max_length=12),
                ),
                ("facility", models.PositiveSmallIntegerField(default=16)),
                ("last_success_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, max_length=80)),
            ],
        ),
        migrations.CreateModel(
            name="LogForwardingDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("audit_event_id", models.PositiveBigIntegerField()),
                ("sequence", models.PositiveIntegerField(default=1)),
                ("payload", models.JSONField(default=dict)),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("sending", "Sending"), ("sent", "Sent")],
                        db_index=True,
                        default="pending",
                        max_length=12,
                    ),
                ),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("next_attempt_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("claimed_at", models.DateTimeField(blank=True, null=True)),
                ("delivered_at", models.DateTimeField(blank=True, null=True)),
                ("last_error_code", models.CharField(blank=True, max_length=80)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["id"]},
        ),
        migrations.AddConstraint(
            model_name="logforwardingdelivery",
            constraint=models.UniqueConstraint(
                fields=("audit_event_id", "sequence"), name="uniq_log_delivery_event_sequence"
            ),
        ),
    ]
