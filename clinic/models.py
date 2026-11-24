import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models.functions import Now
from django.utils import timezone


class AliveManager(models.Manager):
    """The default manager: rows that were deleted (soft) are invisible to every ordinary query."""

    def get_queryset(self):
        return super().get_queryset().filter(deleted_at__isnull=True)


class SyncMixin(models.Model):
    """Identity and change-tracking columns every synced row carries (see docs/OFFLINE_SYNC_CONTRACT.md).

    `sync_uuid` is stable across the cloud and a clinic node, so a retried push never duplicates a row.
    It is nullable on purpose: a row written by an older backend that does not know the column gets
    its uuid filled in later. `deleted_at` is a soft-delete marker so a delete made offline is not
    undone by the next pull.
    """

    sync_uuid = models.UUIDField(default=uuid.uuid4, null=True, unique=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True, db_default=Now())
    deleted_at = models.DateTimeField(null=True, blank=True)

    # `objects` hides deleted rows; `all_objects` (used by the sync engine) sees them as tombstones.
    objects = AliveManager()
    all_objects = models.Manager()

    # A column whose value must stay unique (a code or a name). It is renamed on delete so the same
    # code or name can be used again, while the tombstone keeps its identity for sync.
    SOFT_DELETE_RENAME = ""

    class Meta:
        abstract = True

    def soft_delete(self):
        """Delete without losing the fact that it was deleted: the other side of a sync must learn of it."""
        if self.SOFT_DELETE_RENAME:
            field = self._meta.get_field(self.SOFT_DELETE_RENAME)
            suffix = f" [deleted {uuid.uuid4().hex[:6]}]"
            room = (field.max_length or 255) - len(suffix)
            setattr(self, self.SOFT_DELETE_RENAME, str(getattr(self, self.SOFT_DELETE_RENAME))[:room] + suffix)
        self.deleted_at = timezone.now()
        self.save()


class Department(SyncMixin):
    SOFT_DELETE_RENAME = "name"
    clinic_tin = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=120)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("clinic_tin", "name", "branch_name")

    def __str__(self):
        return self.name


class ClinicBranch(SyncMixin):
    # clinic_tin = organization TIN (groups siblings for referrals / Apex).
    clinic_tin = models.CharField(max_length=50, db_index=True)
    # branch_tin = this location's operational TIN (may differ from org TIN).
    branch_tin = models.CharField(max_length=50, blank=True, default="", db_index=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=255, blank=True, default="")
    is_main = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("clinic_tin", "name")
        ordering = ["-is_main", "name"]

    def __str__(self):
        return self.name

    def operational_tin(self) -> str:
        return (self.branch_tin or self.clinic_tin or "").strip()


class Patient(SyncMixin):
    GENDER_CHOICES = (("Male", "Male"), ("Female", "Female"), ("Other", "Other"))

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    mrn = models.CharField(max_length=40)
    full_name = models.CharField(max_length=255)
    age = models.PositiveIntegerField(default=0)
    gender = models.CharField(max_length=16, choices=GENDER_CHOICES, default="Female")
    phone = models.CharField(max_length=32, blank=True)
    address = models.CharField(max_length=255, blank=True)
    allergies = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("clinic_tin", "mrn", "branch_name")
        ordering = ["-created_at"]


class Encounter(SyncMixin):
    STATUS_OPEN = "open"
    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    ARRIVAL_CHOICES = (("new", "New"), ("returning", "Returning"))

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    number = models.CharField(max_length=40, db_index=True)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="encounters")
    arrival_type = models.CharField(max_length=20, choices=ARRIVAL_CHOICES)
    status = models.CharField(max_length=20, default=STATUS_OPEN, db_index=True)
    referral_source = models.CharField(max_length=255, blank=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="opened_encounters"
    )

    class Meta:
        unique_together = ("clinic_tin", "number", "branch_name")
        ordering = ["-opened_at"]


class BillableService(SyncMixin):
    SOFT_DELETE_RENAME = "code"
    SERVICE_TYPES = (
        ("consultation", "Consultation"),
        ("lab", "Lab"),
        ("radiology", "Radiology"),
        ("pharmacy", "Pharmacy"),
        ("procedure", "Procedure"),
        ("nursing", "Nursing"),
        ("other", "Other"),
    )

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    department = models.CharField(max_length=120)
    service_type = models.CharField(max_length=32, choices=SERVICE_TYPES, default="other")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    default_quantity = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    auto_add_on_registration = models.BooleanField(default=False)
    requires_payment_before_work = models.BooleanField(default=True)
    internal_notes = models.TextField(blank=True)

    class Meta:
        unique_together = ("clinic_tin", "code", "branch_name")


class BillableItem(SyncMixin):
    PAYMENT = (
        ("AwaitingPayment", "AwaitingPayment"),
        ("PartialPayment", "PartialPayment"),
        ("PaymentApproved", "PaymentApproved"),
    )
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="billables")
    service = models.ForeignKey(BillableService, on_delete=models.PROTECT, null=True, blank=True)
    description = models.CharField(max_length=255)
    department = models.CharField(max_length=40)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_status = models.CharField(max_length=32, default="AwaitingPayment")

    @property
    def total_amount(self):
        return self.unit_price * self.quantity


class PaymentTransaction(SyncMixin):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="payments")
    receipt_number = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tender_method = models.CharField(max_length=20)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RefundTransaction(SyncMixin):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="refunds")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="OpenRefund")


class ClinicalOrder(SyncMixin):
    ORDER_TYPES = (("lab", "lab"), ("radiology", "radiology"), ("prescription", "prescription"))
    STATUSES = (
        ("Draft", "Draft"),
        ("AwaitingPayment", "AwaitingPayment"),
        ("PaymentApproved", "PaymentApproved"),
        ("InProgress", "InProgress"),
        ("Completed", "Completed"),
        ("Reviewed", "Reviewed"),
        ("Dispensed", "Dispensed"),
    )
    FULFILLMENT_CLINIC = "clinic_pharmacy"
    FULFILLMENT_EXTERNAL = "external_print"
    FULFILLMENT_CHOICES = (
        (FULFILLMENT_CLINIC, "Clinic pharmacy"),
        (FULFILLMENT_EXTERNAL, "External print"),
    )

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="orders")
    order_type = models.CharField(max_length=20, choices=ORDER_TYPES)
    status = models.CharField(max_length=32, default="AwaitingPayment")
    details = models.TextField()
    result_text = models.TextField(blank=True)
    fulfillment = models.CharField(
        max_length=32, choices=FULFILLMENT_CHOICES, default=FULFILLMENT_CLINIC, blank=True
    )
    medicine = models.ForeignKey(
        "Medicine", on_delete=models.SET_NULL, null=True, blank=True, related_name="orders"
    )
    # Prescriptions: how much to sell, counted in whole packs or loose units (tablets...).
    quantity = models.PositiveIntegerField(default=1, db_default=1)
    sale_unit = models.CharField(max_length=8, default="pack", db_default="pack")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    billable = models.ForeignKey(BillableItem, on_delete=models.SET_NULL, null=True, blank=True)


class DoctorChart(SyncMixin):
    encounter = models.OneToOneField(Encounter, on_delete=models.CASCADE, related_name="chart")
    chief_complaint = models.TextField(blank=True)
    examination = models.TextField(blank=True)
    diagnosis = models.CharField(max_length=255, blank=True)
    clinical_notes = models.TextField(blank=True)
    treatment_plan = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class NurseNote(SyncMixin):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="nurse_notes")
    note_type = models.CharField(max_length=40, default="progress")
    content = models.TextField()
    vitals = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Appointment(SyncMixin):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="appointments")
    scheduled_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Supplier(SyncMixin):
    SOFT_DELETE_RENAME = "supplier_name"
    """Who a medicine stock line was bought from (per clinic branch)."""

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    supplier_name = models.CharField(max_length=255)
    supplier_phone = models.CharField(max_length=32)
    supplier_email = models.EmailField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["supplier_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["clinic_tin", "branch_name", "supplier_name"],
                name="uniq_supplier_name_per_branch",
            )
        ]

    def __str__(self):
        return self.supplier_name


class Medicine(SyncMixin):
    """One medicine stock line: a batch of one product bought from one supplier."""

    UNITS = (
        ("litre", "Litre"),
        ("kilogram", "Kilogram"),
        ("piece", "Piece"),
        ("packet", "Packet"),
        ("box", "Box"),
        ("dozen", "Dozen"),
        ("other", "Other"),
    )
    CATEGORIES = (
        ("pain_fever", "Pain & fever"),
        ("antibiotics", "Antibiotics"),
        ("malaria", "Malaria medicines"),
        ("deworming", "Deworming (worms)"),
        ("diarrhea_ors", "Diarrhea & ORS"),
        ("stomach_acid_ulcer", "Stomach acid / ulcer"),
        ("allergy", "Allergy & itching"),
        ("cough_cold", "Cough & cold"),
        ("asthma", "Asthma / breathing"),
        ("hypertension", "Blood pressure"),
        ("diabetes", "Diabetes"),
        ("family_planning", "Family planning"),
        ("pregnancy", "Pregnancy / mothers"),
        ("skin", "Skin"),
        ("fungal", "Fungal"),
        ("eye_ear", "Eye / ear"),
        ("vitamins", "Vitamins"),
        ("other", "Other"),
    )

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    supplier = models.ForeignKey(
        Supplier, on_delete=models.SET_NULL, null=True, blank=True, related_name="medicines"
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=40, blank=True)  # legacy: no longer shown
    category = models.CharField(max_length=32, choices=CATEGORIES, default="other")
    batch_number = models.CharField(max_length=64, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    # The pack unit stock is counted in (box, packet, piece...). Column name kept for compatibility.
    unit_of_measure = models.CharField(max_length=32, choices=UNITS, default="piece")
    # Whole packs on hand (the "Quantity" the manager registers).
    on_hand = models.IntegerField(default=0)
    min_threshold = models.IntegerField(default=5)
    # Sale price of one whole pack.
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost = models.DecimalField(max_digits=10, decimal_places=2, default=0, db_default=0)
    # Optional split selling: a pack holds `pack_size` loose units (tablets...).
    # `on_hand` counts whole packs; `loose_stock` is what is left of an opened pack.
    pack_size = models.PositiveIntegerField(default=1, db_default=1)
    loose_unit = models.CharField(max_length=24, blank=True, default="", db_default="")
    loose_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    loose_stock = models.PositiveIntegerField(default=0, db_default=0)
    # What was bought in total, and how much of it has been paid for (the rest is supplier credit).
    received_quantity = models.PositiveIntegerField(default=0, db_default=0)
    paid_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, db_default=0)
    image_url = models.URLField(max_length=500, blank=True, default="", db_default="")
    is_active = models.BooleanField(default=True)
    internal_notes = models.TextField(blank=True)  # legacy: no longer shown

    class Meta:
        ordering = ["name"]

    def save(self, *args, **kwargs):
        # A line is active while it has stock; a finished line drops out of the lists.
        self.is_active = self.on_hand > 0 or self.loose_stock > 0
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and {"on_hand", "loose_stock"} & set(update_fields):
            kwargs["update_fields"] = list(set(update_fields) | {"is_active"})
        super().save(*args, **kwargs)


class MedicineDamage(SyncMixin):
    """Stock written off as damaged (broken, expired, spoiled...). Recording one deducts the stock."""

    SALE_UNITS = (("pack", "Pack"), ("loose", "Loose unit"))

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    medicine = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name="damages")
    quantity = models.PositiveIntegerField()
    sale_unit = models.CharField(max_length=8, choices=SALE_UNITS, default="pack")
    reason = models.CharField(max_length=255, blank=True, default="")
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class EquipmentTicket(SyncMixin):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    title = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="Open")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolution = models.TextField(blank=True)


class Referral(SyncMixin):
    KIND_INTERNAL = "internal"
    KIND_EXTERNAL = "external"
    KIND_CHOICES = (
        (KIND_INTERNAL, "Same organization"),
        (KIND_EXTERNAL, "Outside organization"),
    )
    APPROVAL_PENDING = "pending"
    APPROVAL_APPROVED = "approved"
    APPROVAL_REJECTED = "rejected"
    APPROVAL_CHOICES = (
        (APPROVAL_PENDING, "Pending manager approval"),
        (APPROVAL_APPROVED, "Approved"),
        (APPROVAL_REJECTED, "Rejected"),
    )

    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="referrals")
    destination_kind = models.CharField(
        max_length=20, choices=KIND_CHOICES, default=KIND_INTERNAL, db_index=True
    )
    to_department = models.CharField(max_length=120, blank=True)
    to_branch = models.CharField(max_length=120, blank=True)
    external_institution = models.CharField(max_length=255, blank=True, default="")
    diagnosis = models.TextField(blank=True)
    lab_summary = models.TextField(blank=True)
    approval_status = models.CharField(
        max_length=20, choices=APPROVAL_CHOICES, default=APPROVAL_PENDING, db_index=True
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="approved_referrals",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class OrgManagerThread(models.Model):
    """Direct chat between two branch managers in the same organization."""

    org_tin = models.CharField(max_length=50, db_index=True)
    tin_a = models.CharField(max_length=50, db_index=True)
    tin_b = models.CharField(max_length=50, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("org_tin", "tin_a", "tin_b")
        ordering = ["-updated_at"]


class OrgManagerMessage(models.Model):
    thread = models.ForeignKey(OrgManagerThread, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="org_manager_messages"
    )
    sender_tin = models.CharField(max_length=50, db_index=True)
    body = models.TextField(blank=True, default="")
    image_url = models.URLField(blank=True, default="")
    read_by_recipient = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]


class LabTestParameter(SyncMixin):
    """One measurable field of a lab test (e.g. Hemoglobin, Malaria RDT).

    Attached to a lab BillableService so the doctor orders by catalog entry
    and the technician fills structured values instead of free text.
    """

    TYPE_NUMERIC = "numeric"
    TYPE_CHOICE = "choice"
    TYPE_TEXT = "text"
    RESULT_TYPES = (
        (TYPE_NUMERIC, "Numeric"),
        (TYPE_CHOICE, "Choice (e.g. Positive / Negative)"),
        (TYPE_TEXT, "Short text"),
    )

    service = models.ForeignKey(
        BillableService, on_delete=models.CASCADE, related_name="parameters"
    )
    name = models.CharField(max_length=120)
    result_type = models.CharField(max_length=16, choices=RESULT_TYPES, default=TYPE_NUMERIC)
    unit = models.CharField(max_length=32, blank=True)
    ref_low = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    ref_high = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    critical_low = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    critical_high = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    # Comma separated, e.g. "Negative,Positive". abnormal_choices ⊂ choices.
    choices = models.CharField(max_length=255, blank=True)
    abnormal_choices = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "id"]

    @staticmethod
    def _split(raw):
        return [c.strip() for c in (raw or "").split(",") if c.strip()]

    @property
    def choice_list(self):
        return self._split(self.choices)

    @property
    def reference_text(self):
        if self.result_type == self.TYPE_NUMERIC:
            if self.ref_low is not None and self.ref_high is not None:
                return f"{self.ref_low.normalize():f} – {self.ref_high.normalize():f}"
            if self.ref_high is not None:
                return f"< {self.ref_high.normalize():f}"
            if self.ref_low is not None:
                return f"> {self.ref_low.normalize():f}"
            return ""
        if self.result_type == self.TYPE_CHOICE:
            normal = [c for c in self.choice_list if c not in self._split(self.abnormal_choices)]
            return " / ".join(normal)
        return ""

    def evaluate(self, raw_value):
        """Return (clean_value, flag). Raises ValueError for invalid input."""
        value = (raw_value if raw_value is not None else "").__str__().strip()
        if not value:
            raise ValueError(f"{self.name}: value required.")
        if self.result_type == self.TYPE_NUMERIC:
            try:
                number = Decimal(value)
            except Exception:
                raise ValueError(f"{self.name}: enter a number.")
            flag = "normal"
            if self.critical_low is not None and number < self.critical_low:
                flag = "critical"
            elif self.critical_high is not None and number > self.critical_high:
                flag = "critical"
            elif self.ref_low is not None and number < self.ref_low:
                flag = "low"
            elif self.ref_high is not None and number > self.ref_high:
                flag = "high"
            return value, flag
        if self.result_type == self.TYPE_CHOICE:
            if value not in self.choice_list:
                raise ValueError(f"{self.name}: choose one of {', '.join(self.choice_list)}.")
            return value, "abnormal" if value in self._split(self.abnormal_choices) else "normal"
        return value[:255], "normal"


class OrderResultValue(SyncMixin):
    """Recorded value for one parameter of a lab order (snapshot of the definition)."""

    FLAGS = (
        ("normal", "Normal"),
        ("low", "Low"),
        ("high", "High"),
        ("critical", "Critical"),
        ("abnormal", "Abnormal"),
    )

    order = models.ForeignKey(ClinicalOrder, on_delete=models.CASCADE, related_name="result_values")
    parameter = models.ForeignKey(
        LabTestParameter, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    name = models.CharField(max_length=120)
    unit = models.CharField(max_length=32, blank=True)
    reference_text = models.CharField(max_length=120, blank=True)
    value = models.CharField(max_length=255)
    flag = models.CharField(max_length=16, choices=FLAGS, default="normal")
    sort_order = models.PositiveIntegerField(default=0)
    entered_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]


class RadiologyReport(SyncMixin):
    """Structured imaging report for a radiology order."""

    TAGS = (("normal", "Normal"), ("abnormal", "Abnormal"), ("critical", "Critical"))

    order = models.OneToOneField(
        ClinicalOrder, on_delete=models.CASCADE, related_name="radiology_report"
    )
    technique = models.TextField(blank=True)
    findings = models.TextField()
    impression = models.TextField()
    tag = models.CharField(max_length=16, choices=TAGS, default="normal")
    # List of {"url": "https://...", "name": "..."} (images uploaded by the unit).
    attachments = models.JSONField(default=list, blank=True)
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    updated_at = models.DateTimeField(auto_now=True)


class StockMovement(models.Model):
    """Append-only record of every change to a medicine's stock.

    `delta` is counted in the smallest unit the medicine is sold in: loose units when the pack
    splits (pack_size > 1), whole packs otherwise. Summing a medicine's deltas always equals its
    stock on hand, so a sync merges movements instead of copying a stock number.
    """

    KIND_OPENING = "opening"
    KIND_RECEIVE = "receive"
    KIND_DISPENSE = "dispense"
    KIND_DAMAGE = "damage"
    KIND_ADJUST = "adjust"
    KINDS = (
        (KIND_OPENING, "Opening balance"),
        (KIND_RECEIVE, "Received"),
        (KIND_DISPENSE, "Dispensed"),
        (KIND_DAMAGE, "Damaged"),
        (KIND_ADJUST, "Adjustment"),
    )

    sync_uuid = models.UUIDField(default=uuid.uuid4, null=True, unique=True, editable=False)
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    medicine = models.ForeignKey(Medicine, on_delete=models.CASCADE, related_name="movements")
    kind = models.CharField(max_length=16, choices=KINDS, default=KIND_ADJUST)
    delta = models.IntegerField()
    pack_size = models.PositiveIntegerField(default=1)
    reference = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["medicine", "created_at"])]


class SyncState(models.Model):
    """Where a clinic node (one `(clinic_tin, branch_name)` scope) stands with the cloud."""

    clinic_tin = models.CharField(max_length=50)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    seeded_at = models.DateTimeField(null=True, blank=True)
    last_successful_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_token = models.CharField(max_length=128, blank=True, default="")
    # Set when the node was restored from a backup or rebuilt: the first thing to do is sync.
    restore_pending = models.BooleanField(default=False, db_default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["clinic_tin", "branch_name"], name="uniq_sync_state_scope")
        ]


class SyncRun(models.Model):
    """One sync attempt: push, then pull. `sync_uuid` doubles as the push id for idempotency."""

    KIND_SYNC = "sync"
    KIND_SEED = "seed"
    KINDS = ((KIND_SYNC, "Sync"), (KIND_SEED, "Initial seed"))
    STEP_IDLE = "idle"
    STEP_RUNNING = "running"
    STEP_OK = "ok"
    STEP_FAILED = "failed"
    STEP_SKIPPED = "skipped"
    STEPS = (
        (STEP_IDLE, "Idle"),
        (STEP_RUNNING, "Running"),
        (STEP_OK, "OK"),
        (STEP_FAILED, "Failed"),
        (STEP_SKIPPED, "Skipped"),
    )

    sync_uuid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    kind = models.CharField(max_length=8, choices=KINDS, default=KIND_SYNC)
    push_status = models.CharField(max_length=10, choices=STEPS, default=STEP_IDLE)
    pull_status = models.CharField(max_length=10, choices=STEPS, default=STEP_IDLE)
    error_message = models.TextField(blank=True, default="")
    receipt = models.JSONField(default=dict, blank=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class SyncNode(models.Model):
    """A clinic node's credential. One per `(clinic_tin, branch_name)` scope; only the token hash is kept."""

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    label = models.CharField(max_length=120, blank=True, default="")
    token_hash = models.CharField(max_length=64, unique=True)
    token_prefix = models.CharField(max_length=12, blank=True, default="")
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.clinic_tin} / {self.branch_name or 'Main'} ({self.token_prefix}...)"


class SyncConflict(models.Model):
    """A row the merge refused to overwrite silently. Kept for the manager to review."""

    SIDE_CLOUD = "cloud"
    SIDE_NODE = "node"

    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    entity = models.CharField(max_length=40)
    row_uuid = models.CharField(max_length=40, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")
    side = models.CharField(max_length=8, default=SIDE_CLOUD)
    incoming = models.JSONField(default=dict, blank=True)
    existing = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.CharField(max_length=150, blank=True, default="", db_default="")
    resolution_note = models.CharField(max_length=255, blank=True, default="", db_default="")

    class Meta:
        ordering = ["-created_at"]
