from django.conf import settings
from django.db import models


class TenantAccount(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_SUSPENDED = "suspended"
    STATUS_BANNED = "banned"
    STATUS_DELETED = "deleted"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_SUSPENDED, "Suspended"),
        (STATUS_BANNED, "Banned"),
        (STATUS_DELETED, "Deleted"),
    ]

    clinic_tin = models.CharField(max_length=50, unique=True, db_index=True)
    clinic_name = models.CharField(max_length=255, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    logo_url = models.URLField(blank=True, default="")
    branch_name = models.CharField(max_length=255, default="Main")
    account_status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        db_index=True,
    )
    status_reason = models.TextField(blank=True, default="")
    status_changed_at = models.DateTimeField(null=True, blank=True)

    setup_fee_etb = models.PositiveIntegerField(default=15000)
    quarterly_fee_etb = models.PositiveIntegerField(default=5000)
    payment_channel = models.CharField(max_length=64, blank=True, default="")
    payment_transaction_ref = models.CharField(max_length=128, blank=True, default="")
    setup_fee_approved = models.BooleanField(default=False)
    subscription_payment_approved = models.BooleanField(default=False)
    subscription_paid_until = models.DateTimeField(null=True, blank=True)
    paid_quarters_count = models.PositiveIntegerField(default=0)
    billing_hold = models.BooleanField(default=False)
    billing_started_at = models.DateTimeField(null=True, blank=True)
    free_trial_ends_at = models.DateTimeField(null=True, blank=True)
    is_illustration = models.BooleanField(default=False)
    billing_notes = models.TextField(blank=True, default="")
    fees_manually_set = models.BooleanField(default=False)
    yearly_fee_etb = models.PositiveIntegerField(default=0)
    modules = models.JSONField(default=list, blank=True)
    OPS_MODE_ONLINE = "online"
    OPS_MODE_OFFLINE = "offline"
    OPS_MODE_CHOICES = [
        (OPS_MODE_ONLINE, "Online (cloud)"),
        (OPS_MODE_OFFLINE, "Offline with evening sync"),
    ]
    ops_mode = models.CharField(
        max_length=20,
        choices=OPS_MODE_CHOICES,
        default=OPS_MODE_ONLINE,
        db_index=True,
    )
    sales_agent = models.ForeignKey(
        "SalesAgent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tenants",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.clinic_name or self.clinic_tin} ({self.account_status})"


class TenantPaymentSubmission(models.Model):
    KIND_SETUP = "setup"
    KIND_QUARTERLY = "quarterly"
    KIND_CHOICES = [
        (KIND_SETUP, "Setup"),
        (KIND_QUARTERLY, "Quarterly"),
    ]

    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    clinic_tin = models.CharField(max_length=50, db_index=True)
    payment_kind = models.CharField(max_length=20, choices=KIND_CHOICES, db_index=True)
    amount_etb = models.PositiveIntegerField()
    payment_channel = models.CharField(max_length=64)
    transaction_ref = models.CharField(max_length=128)
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_submissions",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_payment_submissions",
    )
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="rejected_payment_submissions",
    )
    rejection_reason = models.TextField(blank=True, default="")
    quarter_number = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [
            models.Index(fields=["clinic_tin", "payment_kind", "status"]),
        ]

    def __str__(self):
        return f"{self.clinic_tin} {self.payment_kind} {self.status}"


class SalesAgent(models.Model):
    display_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=64, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    city = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_name"]

    def __str__(self):
        return self.display_name


class TenantOpsModeChangeRequest(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_APPLIED = "applied"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
        (STATUS_APPLIED, "Applied"),
    ]

    clinic_tin = models.CharField(max_length=50, db_index=True)
    current_ops_mode = models.CharField(max_length=20)
    requested_ops_mode = models.CharField(max_length=20)
    request_note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        db_index=True,
    )
    requested_by_username = models.CharField(max_length=150, blank=True, default="")
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ops_mode_requests_submitted",
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ops_mode_requests_reviewed",
    )
    review_note = models.TextField(blank=True, default="")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["clinic_tin", "status"]),
        ]

    def __str__(self):
        return f"{self.clinic_tin} {self.current_ops_mode}→{self.requested_ops_mode} ({self.status})"


class SubscriptionPricingRule(models.Model):
    """Shared pricing catalog table (managed by mediflow_admin migrations)."""

    business_type = models.CharField(max_length=64, db_index=True)
    modules_key = models.CharField(max_length=255, db_index=True)
    modules = models.JSONField(default=list, blank=True)
    setup_fee_etb = models.PositiveIntegerField(default=15000)
    quarterly_fee_etb = models.PositiveIntegerField(default=5000)
    yearly_fee_etb = models.PositiveIntegerField(default=0)
    description = models.CharField(max_length=255, blank=True, default="")
    is_active = models.BooleanField(default=True, db_index=True)
    sort_order = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "tenants_subscriptionpricingrule"
        ordering = ["sort_order", "business_type", "modules_key"]

    def __str__(self):
        return f"{self.business_type} [{self.modules_key}]"


class TenantFeedbackThread(models.Model):
    """Shared Apex chat thread table (managed by mediflow_admin migrations)."""

    STATUS_OPEN = "open"
    STATUS_CLOSED = "closed"

    pharmacy_tin = models.CharField(max_length=50, unique=True, db_index=True)
    status = models.CharField(max_length=20, default=STATUS_OPEN, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        managed = False
        db_table = "tenants_tenantfeedbackthread"
        ordering = ["-updated_at"]


class TenantFeedbackMessage(models.Model):
    """Shared Apex chat message table (managed by mediflow_admin migrations)."""

    SIDE_TENANT = "tenant"
    SIDE_APEX = "apex"

    thread = models.ForeignKey(
        TenantFeedbackThread,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender_side = models.CharField(max_length=20)
    body = models.TextField()
    image_url = models.URLField(blank=True, default="")
    sender_username = models.CharField(max_length=150, blank=True, default="")
    read_by_tenant = models.BooleanField(default=False)
    read_by_apex = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = False
        db_table = "tenants_tenantfeedbackmessage"
        ordering = ["created_at"]
