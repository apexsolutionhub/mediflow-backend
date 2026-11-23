from decimal import Decimal

from django.conf import settings
from django.db import models


class Department(models.Model):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=120)
    branch_name = models.CharField(max_length=120, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("clinic_tin", "name", "branch_name")

    def __str__(self):
        return self.name


class ClinicBranch(models.Model):
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


class Patient(models.Model):
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


class Encounter(models.Model):
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


class BillableService(models.Model):
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


class BillableItem(models.Model):
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


class PaymentTransaction(models.Model):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="payments")
    receipt_number = models.CharField(max_length=40)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    tender_method = models.CharField(max_length=20)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class RefundTransaction(models.Model):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="refunds")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=255)
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default="OpenRefund")


class ClinicalOrder(models.Model):
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


class DoctorChart(models.Model):
    encounter = models.OneToOneField(Encounter, on_delete=models.CASCADE, related_name="chart")
    chief_complaint = models.TextField(blank=True)
    examination = models.TextField(blank=True)
    diagnosis = models.CharField(max_length=255, blank=True)
    clinical_notes = models.TextField(blank=True)
    treatment_plan = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class NurseNote(models.Model):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="nurse_notes")
    note_type = models.CharField(max_length=40, default="progress")
    content = models.TextField()
    vitals = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Appointment(models.Model):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="appointments")
    scheduled_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Supplier(models.Model):
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


class Medicine(models.Model):
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


class MedicineDamage(models.Model):
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


class EquipmentTicket(models.Model):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    branch_name = models.CharField(max_length=120, blank=True, default="", db_index=True)
    title = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="Open")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolution = models.TextField(blank=True)


class Referral(models.Model):
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


class LabTestParameter(models.Model):
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


class OrderResultValue(models.Model):
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


class RadiologyReport(models.Model):
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
