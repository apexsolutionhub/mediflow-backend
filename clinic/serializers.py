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
    Medicine,
    NurseNote,
    Patient,
    PaymentTransaction,
    Referral,
    RefundTransaction,
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


class ClinicalOrderSerializer(serializers.ModelSerializer):
    patient_name = serializers.CharField(source="encounter.patient.full_name", read_only=True)
    encounter_number = serializers.CharField(source="encounter.number", read_only=True)

    class Meta:
        model = ClinicalOrder
        fields = "__all__"
        read_only_fields = ("created_by",)


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


class MedicineSerializer(serializers.ModelSerializer):
    class Meta:
        model = Medicine
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name")


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
    class Meta:
        model = BillableService
        fields = "__all__"
        read_only_fields = ("clinic_tin", "branch_name")
