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
    clinic_tin = models.CharField(max_length=50, db_index=True)
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


class Patient(models.Model):
    GENDER_CHOICES = (("Male", "Male"), ("Female", "Female"), ("Other", "Other"))

    clinic_tin = models.CharField(max_length=50, db_index=True)
    mrn = models.CharField(max_length=40)
    full_name = models.CharField(max_length=255)
    age = models.PositiveIntegerField(default=0)
    gender = models.CharField(max_length=16, choices=GENDER_CHOICES, default="Female")
    phone = models.CharField(max_length=32, blank=True)
    address = models.CharField(max_length=255, blank=True)
    allergies = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("clinic_tin", "mrn")
        ordering = ["-created_at"]


class Encounter(models.Model):
    STATUS_OPEN = "open"
    STATUS_ACTIVE = "active"
    STATUS_CLOSED = "closed"
    ARRIVAL_CHOICES = (("new", "New"), ("returning", "Returning"), ("referred", "Referred"))

    clinic_tin = models.CharField(max_length=50, db_index=True)
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
        unique_together = ("clinic_tin", "number")
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
        unique_together = ("clinic_tin", "code")


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
    patient = models.ForeignKey(Patient, on_delete=models.CASCADE, related_name="appointments")
    scheduled_at = models.DateTimeField()
    reason = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Medicine(models.Model):
    UNITS = (
        ("tablet", "Tablet"),
        ("capsule", "Capsule"),
        ("bottle", "Bottle"),
        ("vial", "Vial"),
        ("tube", "Tube"),
        ("pack", "Pack"),
        ("other", "Other"),
    )
    CATEGORIES = (
        ("antibiotic", "Antibiotic"),
        ("analgesic", "Analgesic"),
        ("antipyretic", "Antipyretic"),
        ("antihypertensive", "Antihypertensive"),
        ("vitamin", "Vitamin / supplement"),
        ("antacid", "Antacid"),
        ("topical", "Topical"),
        ("infusion", "Infusion / injectable"),
        ("other", "Other"),
    )

    clinic_tin = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    sku = models.CharField(max_length=40, blank=True)
    category = models.CharField(max_length=32, choices=CATEGORIES, default="other")
    batch_number = models.CharField(max_length=64, blank=True)
    expiry_date = models.DateField(null=True, blank=True)
    unit_of_measure = models.CharField(max_length=32, choices=UNITS, default="tablet")
    on_hand = models.IntegerField(default=0)
    min_threshold = models.IntegerField(default=5)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    is_active = models.BooleanField(default=True)
    internal_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]


class EquipmentTicket(models.Model):
    clinic_tin = models.CharField(max_length=50, db_index=True)
    title = models.CharField(max_length=255)
    details = models.TextField(blank=True)
    status = models.CharField(max_length=20, default="Open")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    resolution = models.TextField(blank=True)


class Referral(models.Model):
    encounter = models.ForeignKey(Encounter, on_delete=models.CASCADE, related_name="referrals")
    to_department = models.CharField(max_length=120, blank=True)
    to_branch = models.CharField(max_length=120, blank=True)
    diagnosis = models.TextField(blank=True)
    lab_summary = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
