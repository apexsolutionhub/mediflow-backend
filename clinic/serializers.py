from decimal import Decimal

from rest_framework import serializers

from .models import (
    Appointment,
    BillableItem,
    BillableService,
    ClinicBranch,
    ClinicalOrder,
    Department,
    DoctorChart,
    Encounter,
    EquipmentTicket,
    LabTestParameter,
    Medicine,
    NurseNote,
    OrderResultValue,
    Patient,
    MedicineDamage,
    PaymentTransaction,
    RadiologyReport,
    Referral,
    RefundTransaction,
    Supplier,
)


class ClinicBranchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClinicBranch
        fields = "__all__"
        read_only_fields = ("clinic_tin",)


class DepartmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = Department
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name")


class PatientSerializer(serializers.ModelSerializer):
    class Meta:
        model = Patient
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name", "mrn")


class BillableItemSerializer(serializers.ModelSerializer):
    total_amount = serializers.SerializerMethodField()

    def get_total_amount(self, obj):
        return obj.total_amount

    class Meta:
        model = BillableItem
        fields = "__all__"


class PaymentTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = PaymentTransaction
        fields = "__all__"
        read_only_fields = ("receipt_number", "processed_by")


class RefundTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = RefundTransaction
        fields = "__all__"
        read_only_fields = ("processed_by",)


class LabTestParameterSerializer(serializers.ModelSerializer):
    reference_text = serializers.CharField(read_only=True)
    choice_list = serializers.ListField(child=serializers.CharField(), read_only=True)

    class Meta:
        model = LabTestParameter
        fields = (
            "id",
            "name",
            "result_type",
            "unit",
            "ref_low",
            "ref_high",
            "critical_low",
            "critical_high",
            "choices",
            "abnormal_choices",
            "sort_order",
            "reference_text",
            "choice_list",
        )


class OrderResultValueSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderResultValue
        fields = ("id", "parameter", "name", "unit", "reference_text", "value", "flag", "sort_order")


class RadiologyReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = RadiologyReport
        fields = ("technique", "findings", "impression", "tag", "attachments", "updated_at")


class ClinicalOrderSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="encounter.patient.full_name", read_only=True)
    encounter_number = serializers.CharField(source="encounter.number", read_only=True)
    result_values = OrderResultValueSerializer(many=True, read_only=True)
    test_parameters = serializers.SerializerMethodField()
    radiology_report = RadiologyReportSerializer(read_only=True)
    test_name = serializers.SerializerMethodField()
    medicine_name = serializers.SerializerMethodField()
    medicine_on_hand = serializers.SerializerMethodField()
    medicine_unit = serializers.SerializerMethodField()
    medicine_loose_unit = serializers.SerializerMethodField()
    medicine_stock_text = serializers.SerializerMethodField()
    medicine_stock_after_text = serializers.SerializerMethodField()
    medicine_can_dispense = serializers.SerializerMethodField()
    dispense_quantity = serializers.SerializerMethodField()
    abnormal_count = serializers.SerializerMethodField()
    has_critical = serializers.SerializerMethodField()

    def get_medicine_name(self, obj):
        return obj.medicine.name if obj.medicine_id else ""

    def get_medicine_on_hand(self, obj):
        return obj.medicine.on_hand if obj.medicine_id else None

    def get_medicine_unit(self, obj):
        return obj.medicine.unit_of_measure if obj.medicine_id else ""

    def get_medicine_loose_unit(self, obj):
        return obj.medicine.loose_unit if obj.medicine_id else ""

    def get_medicine_stock_text(self, obj):
        from . import stock

        return stock.stock_text(obj.medicine) if obj.medicine_id else ""

    def get_medicine_stock_after_text(self, obj):
        """What the stock will read once this prescription is dispensed ("" if it cannot be)."""
        from copy import copy

        from . import stock

        if not obj.medicine_id or obj.order_type != "prescription":
            return ""
        probe = copy(obj.medicine)
        try:
            stock.deduct(probe, max(1, int(obj.quantity)), obj.sale_unit)
        except stock.StockError:
            return ""
        return stock.stock_text(probe)

    def get_medicine_can_dispense(self, obj):
        from . import stock

        if not obj.medicine_id or obj.order_type != "prescription":
            return False
        if obj.sale_unit == stock.LOOSE and not stock.is_splittable(obj.medicine):
            return False
        return stock.capacity(obj.medicine, obj.sale_unit) >= max(1, int(obj.quantity))

    def get_dispense_quantity(self, obj):
        """Units that will be sold and stocked out (at least 1)."""
        if obj.order_type != "prescription":
            return None
        return max(1, int(obj.quantity))

    def get_test_name(self, obj):
        billable = obj.billable
        if billable is not None:
            if billable.service_id and billable.service.name:
                return billable.service.name
            if billable.description:
                return billable.description
        return (obj.details or "").strip()[:80]

    def _flags(self, obj):
        flags = [v.flag for v in obj.result_values.all()]
        report = getattr(obj, "radiology_report", None)
        if report is not None:
            flags.append(report.tag)
        return flags

    def get_abnormal_count(self, obj):
        return sum(1 for f in self._flags(obj) if f != "normal")

    def get_has_critical(self, obj):
        return "critical" in self._flags(obj)

    def get_test_parameters(self, obj):
        if obj.order_type != "lab" or not obj.billable_id or not obj.billable.service_id:
            return []
        params = obj.billable.service.parameters.all()
        return LabTestParameterSerializer(params, many=True).data

    class Meta:
        model = ClinicalOrder
        fields = "__all__"
        read_only_fields = ("created_by", "quantity", "sale_unit")


class DoctorChartSerializer(serializers.ModelSerializer):
    class Meta:
        model = DoctorChart
        fields = "__all__"


class NurseNoteSerializer(serializers.ModelSerializer):
    class Meta:
        model = NurseNote
        fields = "__all__"
        read_only_fields = ("created_by",)


class AppointmentSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="patient.full_name", read_only=True)

    class Meta:
        model = Appointment
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name", "created_by")


class SupplierSerializer(serializers.ModelSerializer):
    medicine_count = serializers.SerializerMethodField()

    class Meta:
        model = Supplier
        fields = (
            "id",
            "supplier_name",
            "supplier_phone",
            "supplier_email",
            "medicine_count",
            "created_at",
        )
        read_only_fields = ("id", "medicine_count", "created_at")

    def get_medicine_count(self, obj):
        return obj.medicines.count()

    def validate_supplier_name(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter a supplier name.")
        return value

    def validate_supplier_phone(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter a valid phone number.")
        return value


class MedicineSerializer(serializers.ModelSerializer):
    """The medicine registration record: product, supplier, price/cost, stock, pack splitting, credit."""

    supplier_id = serializers.PrimaryKeyRelatedField(
        source="supplier",
        queryset=Supplier.objects.none(),
        allow_null=True,
        required=False,
        write_only=True,
    )
    selected_supplier_id = serializers.IntegerField(source="supplier.id", read_only=True, default=None)
    supplier_name = serializers.SerializerMethodField()
    supplier_phone = serializers.SerializerMethodField()
    supplier_email = serializers.SerializerMethodField()
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2, min_value=Decimal("0.01"))
    cost = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, min_value=Decimal("0")
    )
    on_hand = serializers.IntegerField(min_value=0)
    paid_amount = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, min_value=Decimal("0")
    )
    pack_size = serializers.IntegerField(required=False, min_value=1)
    loose_stock = serializers.IntegerField(required=False, min_value=0)
    loose_price = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False, allow_null=True, min_value=Decimal("0")
    )
    image_url = serializers.URLField(max_length=500, required=False, allow_blank=True)
    total_cost = serializers.SerializerMethodField()
    credit_amount = serializers.SerializerMethodField()
    payment_status = serializers.SerializerMethodField()
    loose_unit_price = serializers.SerializerMethodField()
    stock_text = serializers.SerializerMethodField()

    class Meta:
        model = Medicine
        fields = [
            "id",
            "supplier_id",
            "selected_supplier_id",
            "supplier_name",
            "supplier_phone",
            "supplier_email",
            "name",
            "category",
            "unit_price",
            "cost",
            "on_hand",
            "unit_of_measure",
            "pack_size",
            "loose_unit",
            "loose_price",
            "loose_unit_price",
            "loose_stock",
            "stock_text",
            "received_quantity",
            "paid_amount",
            "total_cost",
            "credit_amount",
            "payment_status",
            "batch_number",
            "expiry_date",
            "description",
            "image_url",
            "min_threshold",
            "is_active",
            "sku",
            "internal_notes",
        ]
        read_only_fields = [
            "id",
            "selected_supplier_id",
            "supplier_name",
            "supplier_phone",
            "supplier_email",
            "received_quantity",
            "total_cost",
            "credit_amount",
            "payment_status",
            "loose_unit_price",
            "stock_text",
            "is_active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        request = self.context.get("request")
        if request is not None and request.user.is_authenticated:
            from .views import branch_of, tin_of

            self.fields["supplier_id"].queryset = Supplier.objects.filter(
                clinic_tin=tin_of(request.user), branch_name__iexact=branch_of(request.user)
            )

    # ---------- validation ----------
    def validate_name(self, value):
        value = (value or "").strip()
        if len(value) < 2:
            raise serializers.ValidationError("Enter the medicine name.")
        return value

    def validate_batch_number(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Enter the batch number.")
        return value

    def validate(self, attrs):
        inst = self.instance
        creating = inst is None

        supplier = attrs.get("supplier") if "supplier" in attrs else getattr(inst, "supplier", None)
        if supplier is None:
            raise serializers.ValidationError({"supplier_id": ["Please select a supplier."]})
        if creating and not (attrs.get("batch_number") or "").strip():
            raise serializers.ValidationError({"batch_number": ["Enter the batch number."]})

        # What was bought (received qty x cost) is the ceiling for what can be paid.
        cost = attrs.get("cost", getattr(inst, "cost", Decimal("0")))
        if creating:
            received = attrs.get("on_hand", 0)
        else:
            new_qty = attrs.get("on_hand", inst.on_hand)
            received = max(0, inst.received_quantity + (new_qty - inst.on_hand))
        total = Decimal(received) * Decimal(cost)
        paid = attrs.get("paid_amount")
        if paid is not None and paid > total:
            raise serializers.ValidationError(
                {"paid_amount": [f"Paid amount cannot exceed the total cost ({total})."]}
            )

        # Split selling: a pack size above 1 turns it on and needs a loose unit.
        size = attrs.get("pack_size", getattr(inst, "pack_size", 1)) or 1
        if size > 1:
            if not (attrs.get("loose_unit", getattr(inst, "loose_unit", "")) or "").strip():
                attrs["loose_unit"] = "piece"
            loose_stock = attrs.get("loose_stock", getattr(inst, "loose_stock", 0)) or 0
            if loose_stock >= size:
                raise serializers.ValidationError(
                    {"loose_stock": [f"Loose stock must be less than one full pack ({size})."]}
                )
        else:
            attrs["pack_size"] = 1
            attrs["loose_unit"] = ""
            attrs["loose_price"] = None
            attrs["loose_stock"] = 0
        return attrs

    # ---------- save ----------
    def create(self, validated_data):
        quantity = validated_data.get("on_hand", 0)
        cost = validated_data.get("cost", Decimal("0"))
        validated_data["received_quantity"] = quantity
        if validated_data.get("paid_amount") is None:
            validated_data["paid_amount"] = Decimal(quantity) * Decimal(cost)
        from . import ledger

        instance = super().create(validated_data)
        ledger.record_movement(instance, "receive", "intake", self._actor())
        return instance

    def update(self, instance, validated_data):
        if "on_hand" in validated_data:
            delta = validated_data["on_hand"] - instance.on_hand
            validated_data["received_quantity"] = max(0, instance.received_quantity + delta)
        from . import ledger

        instance = super().update(instance, validated_data)
        # A manager edit of the stock: more is "received", less is an "adjustment".
        ledger.record_movement(instance, None, "manager-edit", self._actor())
        return instance

    def _actor(self):
        request = self.context.get("request")
        return getattr(request, "user", None)

    # ---------- computed ----------
    def _total(self, obj):
        return (Decimal(obj.received_quantity) * Decimal(obj.cost)).quantize(Decimal("0.01"))

    @staticmethod
    def _money(value):
        """Money in responses is always a 2-decimal string, like the DecimalFields."""
        return f"{Decimal(value).quantize(Decimal('0.01'))}"

    def get_total_cost(self, obj):
        return self._money(self._total(obj))

    def _credit(self, obj):
        owed = self._total(obj) - Decimal(obj.paid_amount)
        return owed if owed > Decimal("0.01") else Decimal("0.00")

    def get_credit_amount(self, obj):
        return self._money(self._credit(obj))

    def get_payment_status(self, obj):
        return "credit" if self._credit(obj) > 0 else "paid"

    def get_loose_unit_price(self, obj):
        from . import stock

        return self._money(stock.loose_unit_price(obj)) if stock.is_splittable(obj) else None

    def get_stock_text(self, obj):
        from . import stock

        return stock.stock_text(obj)

    def get_supplier_name(self, obj):
        return obj.supplier.supplier_name if obj.supplier_id else ""

    def get_supplier_phone(self, obj):
        return obj.supplier.supplier_phone if obj.supplier_id else ""

    def get_supplier_email(self, obj):
        return obj.supplier.supplier_email if obj.supplier_id else ""


class MedicineDamageSerializer(serializers.ModelSerializer):
    medicine_name = serializers.CharField(source="medicine.name", read_only=True)
    quantity_text = serializers.SerializerMethodField()
    recorded_by_name = serializers.SerializerMethodField()

    class Meta:
        model = MedicineDamage
        fields = [
            "id",
            "medicine",
            "medicine_name",
            "quantity",
            "sale_unit",
            "quantity_text",
            "reason",
            "recorded_by_name",
            "created_at",
        ]
        read_only_fields = ["id", "medicine_name", "quantity_text", "recorded_by_name", "created_at"]
        extra_kwargs = {"quantity": {"min_value": 1}}

    def get_quantity_text(self, obj):
        from . import stock

        word = stock.loose_label(obj.medicine) if obj.sale_unit == "loose" else stock.pack_label(obj.medicine)
        return f"{obj.quantity} {stock._plural(word, obj.quantity)}"

    def get_recorded_by_name(self, obj):
        return obj.recorded_by.get_username() if obj.recorded_by_id else ""


class EquipmentTicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = EquipmentTicket
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name", "created_by")


class ReferralSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="encounter.patient.full_name", read_only=True)
    encounter_number = serializers.CharField(source="encounter.number", read_only=True)
    created_by_name = serializers.SerializerMethodField()
    approved_by_name = serializers.SerializerMethodField()

    class Meta:
        model = Referral
        fields = "__all__"
        read_only_fields = (
            "created_by",
            "approval_status",
            "approved_by",
            "approved_at",
        )

    def get_created_by_name(self, obj):
        user = obj.created_by
        return getattr(user, "username", "") if user else ""

    def get_approved_by_name(self, obj):
        user = obj.approved_by
        return getattr(user, "username", "") if user else ""

    def validate(self, attrs):
        kind = (attrs.get("destination_kind") or getattr(self.instance, "destination_kind", None) or "internal").strip()
        if kind == Referral.KIND_INTERNAL:
            branch = (attrs.get("to_branch") or getattr(self.instance, "to_branch", "") or "").strip()
            dept = (attrs.get("to_department") or getattr(self.instance, "to_department", "") or "").strip()
            if not branch:
                raise serializers.ValidationError({"to_branch": ["Select a destination branch."]})
            if not dept:
                raise serializers.ValidationError({"to_department": ["Select a department."]})
        elif kind == Referral.KIND_EXTERNAL:
            attrs["to_branch"] = ""
            attrs["to_department"] = (attrs.get("to_department") or "").strip()
        else:
            raise serializers.ValidationError({"destination_kind": ["Choose same-org or outside."]})
        return attrs

    def create(self, validated_data):
        validated_data["approval_status"] = Referral.APPROVAL_PENDING
        validated_data["approved_by"] = None
        validated_data["approved_at"] = None
        return super().create(validated_data)


class EncounterSerializer(serializers.ModelSerializer):
    patient = PatientSerializer(read_only=True)
    patient_id = serializers.PrimaryKeyRelatedField(
        queryset=Patient.objects.all(), source="patient", write_only=True, required=False
    )
    billables = BillableItemSerializer(many=True, read_only=True)
    orders = ClinicalOrderSerializer(many=True, read_only=True)
    payments = PaymentTransactionSerializer(many=True, read_only=True)
    refunds = RefundTransactionSerializer(many=True, read_only=True)
    chart = DoctorChartSerializer(read_only=True)
    nurse_notes = NurseNoteSerializer(many=True, read_only=True)
    amount_due = serializers.SerializerMethodField()

    def get_amount_due(self, obj):
        due = 0
        for item in obj.billables.all():
            if item.payment_status == "PaymentApproved":
                continue
            remaining = float(item.total_amount) - float(item.paid_amount or 0)
            if remaining > 0:
                due += remaining
        return round(due, 2)

    class Meta:
        model = Encounter
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name", "number", "opened_by", "closed_at")


class BillableServiceSerializer(serializers.ModelSerializer):
    parameters = LabTestParameterSerializer(many=True, read_only=True)

    class Meta:
        model = BillableService
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name")
