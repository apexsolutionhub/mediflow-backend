import datetime
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from api import audit
from tenants.permissions import TenantBillingAccessPermission

from . import billing, ledger, stock
from . import vitals as vitals_mod
from .duplicates import Conflict, confirmed, text_key, digits, warn_unless_confirmed
from .models import PRIORITIES
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
    MedicineDamage,
    NurseNote,
    OrderResultValue,
    OrgManagerMessage,
    OrgManagerThread,
    Patient,
    RadiologyReport,
    PaymentRule,
    PaymentTransaction,
    ForgivenAmount,
    Referral,
    TreatmentAssignment,
    RefundTransaction,
    Supplier,
)
from .serializers import (
    LabTestParameterSerializer,
    AppointmentSerializer,
    BillableItemSerializer,
    BillableServiceSerializer,
    ClinicBranchSerializer,
    ClinicalOrderSerializer,
    DepartmentSerializer,
    DoctorChartSerializer,
    EncounterSerializer,
    EquipmentTicketSerializer,
    MedicineDamageSerializer,
    MedicineSerializer,
    NurseNoteSerializer,
    PatientSerializer,
    ForgivenAmountSerializer,
    PaymentRuleSerializer,
    PaymentTransactionSerializer,
    ReferralSerializer,
    RefundTransactionSerializer,
    SupplierSerializer,
)

STAFF_ROLES = {"manager", "reception", "doctor", "nurse", "lab", "radiology", "pharmacist"}


class FlexiblePagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 200


def profile_of(user):
    return getattr(user, "profile", None)


def tin_of(user):
    profile = profile_of(user)
    return (getattr(profile, "clinic_tin", "") or "").strip()


def role_of(user):
    profile = profile_of(user)
    return (getattr(profile, "role", "") or "").strip().lower()


def branch_of(user):
    """Staff work inside one branch; org TIN is shared across branches."""
    profile = profile_of(user)
    return (getattr(profile, "branch_name", "") or "").strip() or "Main"


def scope_branch(qs, user, field="branch_name"):
    return qs.filter(**{f"{field}__iexact": branch_of(user)})


def org_tin_of(user):
    from tenants.services import org_tin_for

    return org_tin_for(tin_of(user))


class TenantScopedMixin:
    pagination_class = FlexiblePagination

    def get_permissions(self):
        return [IsAuthenticated(), TenantBillingAccessPermission()]

    def get_clinic_tin(self):
        return tin_of(self.request.user)

    def get_branch_name(self):
        return branch_of(self.request.user)

    def require_roles(self, *roles):
        if role_of(self.request.user) not in {r.lower() for r in roles}:
            return False
        return True

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        # Every successful change through a clinic screen is written to the audit log with who did it.
        audit.record_from_view(self, request, response)
        return response

    def perform_destroy(self, instance):
        # Deleted rows stay as tombstones so a sync can carry the deletion to the other side.
        if hasattr(instance, "soft_delete"):
            instance.soft_delete()
        else:
            instance.delete()


class PatientViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = PatientSerializer

    def get_queryset(self):
        qs = Patient.objects.filter(clinic_tin=self.get_clinic_tin())
        qs = scope_branch(qs, self.request.user)
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(mrn__icontains=q) | Q(phone__icontains=q))
        return qs

    def _already_registered(self, data, tin, branch):
        """The patient this registration describes, if the clinic already has them.

        Same name and the same phone is the same person. With no phone on either side, same name, age and gender.
        """
        name = text_key(data.get("full_name"))
        if not name:
            return None
        phone = digits(data.get("phone"))
        first_word = name.split(" ")[0]
        pool = Patient.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, full_name__icontains=first_word
        )
        for other in pool:
            if text_key(other.full_name) != name:
                continue
            other_phone = digits(other.phone)
            if phone or other_phone:
                if phone and phone == other_phone:
                    return other
            elif str(other.age) == str(data.get("age") or 0) and (other.gender or "") == (data.get("gender") or other.gender):
                return other
        return None

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        existing = self._already_registered(serializer.validated_data, tin, branch)
        if existing is not None:
            open_visit = existing.encounters.exclude(status=Encounter.STATUS_CLOSED).order_by("-opened_at").first()
            if open_visit is not None:
                message = (
                    f"{existing.full_name} ({existing.mrn}) is already registered and still has an open visit "
                    f"({open_visit.number}). A patient with an open visit cannot be registered again."
                )
            else:
                message = (
                    f"{existing.full_name} ({existing.mrn}) is already registered and has visit history. "
                    "Register them from Returning registration."
                )
            raise Conflict(
                message,
                "patient_exists",
                patient_id=existing.id,
                mrn=existing.mrn,
                open_encounter=open_visit.number if open_visit else None,
            )
        count = Patient.objects.filter(clinic_tin=tin, branch_name__iexact=branch).count() + 1
        serializer.save(clinic_tin=tin, branch_name=branch, mrn=f"MRN-{count:05d}")


def attach_registration_billables(encounter, tin, branch, referral=False):
    """Consultation is billable on arrival: payment unlocks clinical units.

    A referred patient arrives with results from the other branch, so the consultation is only
    charged when the service says it includes referral patients."""
    consult = (
        BillableService.objects.filter(
            clinic_tin=tin,
            branch_name__iexact=branch,
            auto_add_on_registration=True,
            is_active=True,
        ).first()
        or BillableService.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, code="CONSULT", is_active=True
        ).first()
        or BillableService.objects.filter(
            clinic_tin=tin,
            branch_name__iexact=branch,
            department__iexact="Consultation",
            is_active=True,
        ).first()
    )
    if referral and not (consult and consult.includes_referral_patients):
        return
    if consult:
        BillableItem.objects.create(
            encounter=encounter,
            service=consult,
            description=(consult.description or consult.name).strip()[:255],
            department=consult.department or "Consultation",
            unit_price=consult.unit_price,
            quantity=max(1, consult.default_quantity or 1),
            payment_status="AwaitingPayment",
        )
    else:
        BillableItem.objects.create(
            encounter=encounter,
            description="Consultation",
            department="Consultation",
            unit_price=Decimal("300"),
            quantity=1,
            payment_status="AwaitingPayment",
        )


def perform_checkout(encounter, branch):
    """Close the visit and build the print payload (health report, outside prescriptions, follow-ups, referrals)."""
    external_rx = list(
        encounter.orders.filter(
            order_type="prescription", fulfillment=ClinicalOrder.FULFILLMENT_EXTERNAL
        ).select_related("medicine")
    )
    if encounter.status != Encounter.STATUS_CLOSED:
        encounter.status = Encounter.STATUS_CLOSED
        encounter.closed_at = timezone.now()
        encounter.save(update_fields=["status", "closed_at"])
    # What the doctor asked the nurse to measure can no longer be filled once the patient has left.
    encounter.treatment_assignments.filter(status="active").update(status="expired", expired_at=timezone.now())
    payload = EncounterSerializer(encounter).data

    def medicine_label(order):
        if order.medicine_id:
            name = getattr(order.medicine, "name", "") or ""
            if name:
                return name
        details = (order.details or "").strip()
        if details:
            return details.split(" · ")[0].strip()
        return "Medicine"

    payload["external_prescriptions"] = [
        {"id": order.id, "details": order.details, "medicine_name": medicine_label(order)} for order in external_rx
    ]
    upcoming = Appointment.objects.filter(
        clinic_tin=encounter.clinic_tin,
        branch_name__iexact=encounter.branch_name or branch,
        patient_id=encounter.patient_id,
        scheduled_at__gte=timezone.now(),
    ).order_by("scheduled_at")[:20]
    payload["follow_up_appointments"] = AppointmentSerializer(upcoming, many=True).data
    payload["referrals"] = ReferralSerializer(encounter.referrals.all().order_by("created_at"), many=True).data
    return payload


def open_visit_for(patient, arrival, tin, branch, user, priority="normal", discount_rule=None, refund_rule=None, referral=None):
    """Open a visit outside the registration form (a referred patient arriving)."""
    day = timezone.now().strftime("%Y%m%d")
    seq = (
        Encounter.objects.filter(clinic_tin=tin, branch_name__iexact=branch, number__startswith=f"ENC-{day}").count()
        + 1
    )
    encounter = Encounter.objects.create(
        clinic_tin=tin,
        branch_name=branch,
        number=f"ENC-{day}-{seq:04d}",
        patient=patient,
        arrival_type=arrival,
        opened_by=user,
        status=Encounter.STATUS_OPEN,
        priority=priority,
        discount_rule=discount_rule,
        refund_rule=refund_rule,
        referral_from=referral,
        referral_source=(f"Referred from {referral.encounter.branch_name}" if referral else ""),
    )
    attach_registration_billables(encounter, tin, branch, referral=referral is not None)
    return encounter


class EncounterViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EncounterSerializer

    def get_queryset(self):
        qs = (
            Encounter.objects.filter(clinic_tin=self.get_clinic_tin())
            .select_related("patient", "chart")
            .prefetch_related(
                "billables",
                "orders",
                "orders__radiology_report",
                "orders__result_values",
                "orders__billable__service__parameters",
                "orders__medicine",
                "payments",
                "refunds",
                "nurse_notes",
            )
        )
        qs = scope_branch(qs, self.request.user)
        today = self.request.query_params.get("today")
        if today == "1":
            # Reception desk: every non-closed visit must stay visible so payment
            # approvals (lab/rad/Rx added mid-visit) are not lost when the calendar
            # day rolls or timezone offsets shift opened_at.
            qs = qs.exclude(status=Encounter.STATUS_CLOSED)
        status_q = self.request.query_params.get("status")
        if status_q:
            qs = qs.filter(status=status_q)
        board = self.request.query_params.get("board")
        if board == "doctor":
            # Payment gate: doctor works only after reception unlocks the visit (active).
            qs = qs.filter(status=Encounter.STATUS_ACTIVE)
        elif board == "nurse":
            qs = qs.exclude(status=Encounter.STATUS_CLOSED)
        elif board == "open":
            qs = qs.exclude(status=Encounter.STATUS_CLOSED)
        if board or today == "1":
            # Boards: Very high first, then High, then Normal; inside a priority the first to arrive is first.
            qs = qs.annotate(prio=priority_rank("priority")).order_by("prio", "opened_at")
        return qs

    def get_serializer(self, *args, **kwargs):
        serializer = super().get_serializer(*args, **kwargs)
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        target = getattr(serializer, "child", serializer)
        if "patient_id" in getattr(target, "fields", {}):
            target.fields["patient_id"].queryset = Patient.objects.filter(
                clinic_tin=tin, branch_name__iexact=branch
            )
        return serializer

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        day = timezone.now().strftime("%Y%m%d")
        seq = (
            Encounter.objects.filter(
                clinic_tin=tin, branch_name__iexact=branch, number__startswith=f"ENC-{day}"
            ).count()
            + 1
        )
        discount = pick_rule(self, self.request.data.get("discount_rule"), "discount")
        refund = pick_rule(self, self.request.data.get("refund_rule"), "refund")
        patient = serializer.validated_data.get("patient")
        arrival = serializer.validated_data.get("arrival_type") or "new"
        if arrival == "returning" and not patient:
            raise ValueError("Returning patients require an existing record.")
        if patient is not None:
            open_visit = patient.encounters.exclude(status=Encounter.STATUS_CLOSED).order_by("-opened_at").first()
            if open_visit is not None:
                raise Conflict(
                    f"{patient.full_name} still has an open visit ({open_visit.number}). "
                    "Check them out before starting a new one.",
                    "open_encounter",
                    patient_id=patient.id,
                    open_encounter=open_visit.number,
                )
        encounter = serializer.save(
            clinic_tin=tin,
            branch_name=branch,
            number=f"ENC-{day}-{seq:04d}",
            opened_by=self.request.user,
            status=Encounter.STATUS_OPEN,
            discount_rule=discount,
            refund_rule=refund,
        )
        attach_registration_billables(encounter, tin, branch)

    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        encounter = self.get_object()
        unpaid = encounter.billables.exclude(payment_status="PaymentApproved")
        if unpaid.exists():
            return Response(
                {"detail": "Checkout blocked until required billable items are approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(perform_checkout(encounter, self.get_branch_name()))


class BillableServiceViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = BillableServiceSerializer

    def get_queryset(self):
        return scope_branch(
            BillableService.objects.filter(clinic_tin=self.get_clinic_tin()),
            self.request.user,
        )

    def _clear_other_auto_add(self, tin, branch, keep_pk=None):
        qs = BillableService.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, auto_add_on_registration=True
        )
        if keep_pk:
            qs = qs.exclude(pk=keep_pk)
        qs.update(auto_add_on_registration=False)

    SERVICE_IDENTITY = (
        "code", "name", "description", "department", "service_type", "unit_price", "default_quantity",
        "auto_add_on_registration", "requires_payment_before_work", "internal_notes",
    )

    def _refuse_exact_copy(self, data, tin, branch, exclude_pk=None):
        """A billable service whose every field matches an existing one is refused."""
        def norm(field, value):
            return text_key(value) if isinstance(value, str) else str(value)

        pool = BillableService.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, name__iexact=(data.get("name") or "").strip()
        )
        if exclude_pk:
            pool = pool.exclude(pk=exclude_pk)
        for other in pool:
            same = all(
                norm(f, data[f]) == norm(f, getattr(other, f))
                for f in self.SERVICE_IDENTITY
                if f in data
            )
            if same:
                raise ValidationError(
                    {"detail": f"{other.name} is already registered with exactly the same details."}
                )
        same_code = BillableService.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, code__iexact=(data.get("code") or "").strip()
        )
        if exclude_pk:
            same_code = same_code.exclude(pk=exclude_pk)
        if data.get("code") and same_code.exists():
            raise ValidationError({"code": [f"The code {data['code']} is already used by another service."]})

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        self._refuse_exact_copy(serializer.validated_data, tin, branch)
        if serializer.validated_data.get("auto_add_on_registration"):
            self._clear_other_auto_add(tin, branch)
        serializer.save(clinic_tin=tin, branch_name=branch)

    def perform_update(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        merged = {f: getattr(serializer.instance, f) for f in self.SERVICE_IDENTITY}
        merged.update({k: v for k, v in serializer.validated_data.items() if k in self.SERVICE_IDENTITY})
        self._refuse_exact_copy(merged, tin, branch, exclude_pk=serializer.instance.pk)
        if serializer.validated_data.get("auto_add_on_registration"):
            self._clear_other_auto_add(tin, branch, keep_pk=serializer.instance.pk)
        serializer.save()

    def perform_destroy(self, instance):
        if BillableItem.objects.filter(service=instance).exists():
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {
                    "detail": "This service is linked to existing billable items. Deactivate it instead of deleting."
                }
            )
        if instance.auto_add_on_registration:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {
                    "detail": "Cannot delete the auto-add registration service. Assign another service first."
                }
            )
        instance.soft_delete()

    @action(detail=True, methods=["get", "put"])
    def parameters(self, request, pk=None):
        """Read or replace the structured result fields of a lab service."""
        service = self.get_object()
        if request.method == "GET":
            return Response(LabTestParameterSerializer(service.parameters.all(), many=True).data)
        if role_of(request.user) != "manager":
            return Response({"detail": "Only the manager can edit test parameters."}, status=403)
        rows = request.data if isinstance(request.data, list) else request.data.get("parameters")
        if not isinstance(rows, list):
            return Response({"detail": "Send a list of parameters."}, status=400)
        clean = []
        for index, row in enumerate(rows):
            ser = LabTestParameterSerializer(data=row)
            if not ser.is_valid():
                return Response({"detail": f"Row {index + 1}: {ser.errors}"}, status=400)
            data = ser.validated_data
            if data["result_type"] == "choice" and len(LabTestParameter._split(data.get("choices"))) < 2:
                return Response(
                    {"detail": f"Row {index + 1}: choice tests need at least two choices."},
                    status=400,
                )
            clean.append(LabTestParameter(service=service, **{**data, "sort_order": index}))
        with transaction.atomic():
            for old_parameter in LabTestParameter.objects.filter(service=service):
                old_parameter.soft_delete()
            getattr(service, "_prefetched_objects_cache", {}).pop("parameters", None)
            LabTestParameter.objects.bulk_create(clean)
        return Response(LabTestParameterSerializer(service.parameters.all(), many=True).data)


class PaymentViewSet(TenantScopedMixin, viewsets.GenericViewSet):
    serializer_class = PaymentTransactionSerializer

    def _encounter(self, pk):
        return Encounter.objects.filter(
            pk=pk, clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        ).first()

    @staticmethod
    def _parts(request, payable):
        """How the money arrives: [{method, amount}]. The older single method + amount still works."""
        parts = request.data.get("parts")
        if parts is None:
            method = request.data.get("tender_method") or "cash"
            raw = request.data.get("amount")
            return [{"method": method, "amount": billing.money(payable if raw in (None, "") else raw)}]
        if not isinstance(parts, list):
            raise ValidationError({"detail": "Add at least one payment type."})
        clean = []
        for part in parts:
            method = str((part or {}).get("method") or "").strip().lower()
            if method not in {"cash", "bank", "telebirr", "insurance"}:
                raise ValidationError({"detail": f"Unknown payment type: {method or 'blank'}."})
            amount = billing.money((part or {}).get("amount"))
            if amount < 0:
                raise ValidationError({"detail": "A payment part can't be negative."})
            if amount > 0:
                clean.append({"method": method, "amount": amount})
        return clean

    @action(detail=False, methods=["post"])
    def approve(self, request):
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can approve payments."}, status=403)
        encounter = self._encounter(request.data.get("encounter"))
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        due = billing.amount_due(encounter)
        if due <= 0:
            return Response({"detail": "Nothing is due for this visit."}, status=400)

        discount, rule = Decimal("0"), None
        if request.data.get("discount_rule"):
            rule = PaymentRule.objects.filter(
                pk=request.data.get("discount_rule"),
                kind="discount",
                clinic_tin=self.get_clinic_tin(),
                branch_name__iexact=self.get_branch_name(),
            ).first()
            if not rule:
                return Response({"detail": "That discount rule no longer exists."}, status=400)
            discount = billing.discount_for(encounter, rule)
            if discount <= 0:
                return Response(
                    {"detail": f"The {rule.name} rule does not apply to anything still unpaid on this visit."},
                    status=400,
                )

        forgiven, forgiveness = Decimal("0"), billing.open_forgiveness(encounter)
        if forgiveness:
            if forgiveness.status == "pending":
                return Response({"detail": "The forgiven amount is waiting for the manager's approval."}, status=400)
            if forgiveness.status == "declined":
                return Response(
                    {"detail": "The manager declined the forgiven amount. Change it, or remove it, to continue."},
                    status=400,
                )
            forgiven = billing.money(forgiveness.amount)
            if forgiven + discount > due:
                return Response(
                    {"detail": "The forgiven amount and discount are more than is due. Request the forgiven amount again."},
                    status=400,
                )

        waived = discount + forgiven
        payable = due - waived
        parts = self._parts(request, payable)
        cash = sum((p["amount"] for p in parts), Decimal("0"))
        if waived > 0:
            if cash != payable:
                return Response(
                    {"detail": f"With a discount or forgiven amount the payment must be exactly {payable} ETB."},
                    status=400,
                )
        elif cash <= 0:
            return Response({"detail": "Amount must be positive."}, status=400)
        if not parts:  # everything was waived
            parts = [{"method": "waived", "amount": Decimal("0")}]

        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        invoice = f"INV-{stamp}-{encounter.id}"
        who = billing.acting_name(request)
        created = []
        with transaction.atomic():
            billing.settle(encounter, cash + waived)
            for n, part in enumerate(parts, start=1):
                first = n == 1
                created.append(
                    PaymentTransaction.objects.create(
                        encounter=encounter,
                        receipt_number=f"RCPT-{stamp}-{encounter.id}-{n}"[:40],
                        invoice_number=invoice,
                        amount=part["amount"],
                        tender_method=part["method"],
                        processed_by=request.user,
                        processed_by_name=who,
                        notes=request.data.get("notes", ""),
                        vat_amount=billing.vat_of(part["amount"]),
                        discount_amount=discount if first else 0,
                        discount_rule_name=(rule.name if rule and first else ""),
                        discount_reason=(rule.reason if rule and first else ""),
                        forgiven_amount=forgiven if first else 0,
                    )
                )
            if forgiveness and forgiven > 0:
                forgiveness.status = "applied"
                forgiveness.save(update_fields=["status", "updated_at"])
        body = PaymentTransactionSerializer(created[0]).data
        body["payments"] = PaymentTransactionSerializer(created, many=True).data
        return Response(body, status=201)

    @action(detail=False, methods=["post"], url_path="pay-for")
    def pay_for(self, request):
        """One payer settles other patients' open bills in one go; every bill gets its own receipt."""
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can approve payments."}, status=403)
        payer = self._encounter(request.data.get("payer_encounter"))
        if not payer:
            return Response({"detail": "Choose who is paying."}, status=400)
        ids = request.data.get("encounters")
        if not isinstance(ids, list) or not ids:
            return Response({"detail": "Tick at least one bill to pay."}, status=400)
        method = str(request.data.get("tender_method") or "cash")
        stamp = timezone.now().strftime("%Y%m%d%H%M%S")
        invoice = f"INV-{stamp}-{payer.id}P"
        who = billing.acting_name(request)
        made = []
        with transaction.atomic():
            for n, pk in enumerate(dict.fromkeys(ids), start=1):
                target = self._encounter(pk)
                if not target or target.id == payer.id:
                    raise ValidationError({"detail": "Choose other patients' bills, not the payer's own."})
                name = target.patient.full_name
                if billing.open_forgiveness(target):
                    raise ValidationError(
                        {"detail": f"{name} has a forgiven amount open. Settle that bill at their own cashier."}
                    )
                due = billing.amount_due(target)
                if due <= 0:
                    raise ValidationError({"detail": f"{name} has nothing due."})
                billing.settle(target, due)
                made.append(
                    PaymentTransaction.objects.create(
                        encounter=target,
                        receipt_number=f"RCPT-{stamp}-{target.id}-{n}"[:40],
                        invoice_number=invoice,
                        amount=due,
                        tender_method=method,
                        processed_by=request.user,
                        processed_by_name=who,
                        vat_amount=billing.vat_of(due),
                        paid_by_patient=payer.patient,
                        notes=f"Paid by {payer.patient.full_name} ({payer.patient.mrn})",
                    )
                )
        return Response(
            {
                "invoice_number": invoice,
                "total": sum((p.amount for p in made), Decimal("0")),
                "payments": PaymentTransactionSerializer(made, many=True).data,
            },
            status=201,
        )

    @action(detail=False, methods=["post"])
    def forgive(self, request):
        """Ask to waive part of a bill. Anything above 0 goes to the manager; 0 withdraws the request."""
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Forbidden."}, status=403)
        encounter = self._encounter(request.data.get("encounter"))
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        try:
            amount = billing.money(request.data.get("amount"))
        except Exception:
            return Response({"detail": "Enter a valid forgiven amount."}, status=400)
        if amount < 0:
            return Response({"detail": "The forgiven amount can't be less than 0."}, status=400)
        current = billing.open_forgiveness(encounter)
        if amount == 0:
            if current:
                current.status = "superseded"
                current.save(update_fields=["status", "updated_at"])
            return Response({"cancelled": True, "forgiveness": None})
        reason = str(request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "Give a reason for the forgiven amount."}, status=400)
        if amount > billing.amount_due(encounter):
            return Response({"detail": "The forgiven amount can't be more than what is due."}, status=400)
        if current and current.amount == amount and current.reason == reason:
            return Response(ForgivenAmountSerializer(current).data)
        with transaction.atomic():
            if current:
                current.status = "superseded"
                current.save(update_fields=["status", "updated_at"])
            record = ForgivenAmount.objects.create(
                encounter=encounter,
                amount=amount,
                reason=reason[:255],
                requested_by=request.user,
                requested_by_name=billing.acting_name(request),
            )
        return Response(ForgivenAmountSerializer(record).data, status=201)

    @action(detail=False, methods=["get", "post"], url_path="forgiveness")
    def forgiveness(self, request):
        """GET: the manager's list of forgiven amounts. POST: approve or decline one."""
        if role_of(request.user) != "manager":
            return Response({"detail": "Only the manager can approve forgiven amounts."}, status=403)
        scope = {
            "encounter__clinic_tin": self.get_clinic_tin(),
            "encounter__branch_name__iexact": self.get_branch_name(),
        }
        if request.method == "GET":
            rows = (
                ForgivenAmount.objects.filter(**scope)
                .exclude(status="superseded")
                .select_related("encounter__patient")[:200]
            )
            return Response(ForgivenAmountSerializer(rows, many=True).data)
        record = ForgivenAmount.objects.filter(pk=request.data.get("id"), **scope).first()
        if not record:
            return Response({"detail": "Not found."}, status=404)
        if record.status != "pending":
            return Response({"detail": f"This request was already {record.status}."}, status=400)
        decision = str(request.data.get("decision") or "").lower()
        if decision not in {"approve", "decline"}:
            return Response({"detail": "Choose approve or decline."}, status=400)
        record.status = "approved" if decision == "approve" else "declined"
        record.decided_by = request.user
        record.decided_by_name = billing.acting_name(request)
        record.decided_at = timezone.now()
        record.decision_note = str(request.data.get("note") or "")[:255]
        record.save()
        return Response(ForgivenAmountSerializer(record).data)

    @action(detail=False, methods=["get", "post"])
    def refund(self, request):
        """GET: refunds given. POST: the manager gives one (a rule's percentage, or an amount and a reason)."""
        if role_of(request.user) != "manager":
            return Response({"detail": "Only the manager can give a refund."}, status=403)
        scope = {
            "encounter__clinic_tin": self.get_clinic_tin(),
            "encounter__branch_name__iexact": self.get_branch_name(),
        }
        if request.method == "GET":
            rows = RefundTransaction.objects.filter(**scope).select_related("encounter__patient")[:200]
            return Response(RefundTransactionSerializer(rows, many=True).data)
        encounter = self._encounter(request.data.get("encounter"))
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        rule, reason = None, str(request.data.get("reason") or "").strip()
        if request.data.get("rule"):
            rule = PaymentRule.objects.filter(
                pk=request.data.get("rule"),
                kind="refund",
                clinic_tin=self.get_clinic_tin(),
                branch_name__iexact=self.get_branch_name(),
            ).first()
            if not rule:
                return Response({"detail": "That refund rule no longer exists."}, status=400)
            amount = billing.money(billing.refundable_for(encounter, rule) * rule.percent / 100)
            reason = reason or rule.reason
        else:
            amount = billing.money(request.data.get("amount"))
        if amount <= 0:
            return Response({"detail": "The refund amount must be more than 0."}, status=400)
        if not reason:
            return Response({"detail": "Give a reason for the refund."}, status=400)
        if amount > billing.refundable_for(encounter):
            return Response({"detail": "That is more than this visit has paid and not yet refunded."}, status=400)
        refund = RefundTransaction.objects.create(
            encounter=encounter,
            amount=amount,
            reason=reason[:255],
            processed_by=request.user,
            processed_by_name=billing.acting_name(request),
            rule_name=rule.name if rule else "",
        )
        return Response(RefundTransactionSerializer(refund).data, status=201)


class OrderViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = ClinicalOrderSerializer

    def get_queryset(self):
        qs = ClinicalOrder.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        )
        otype = self.request.query_params.get("type")
        queue = self.request.query_params.get("queue")
        if otype:
            qs = qs.filter(order_type=otype)
        if queue == "lab":
            # Active work + recently sent results for the lab results portal.
            qs = qs.filter(
                order_type="lab",
                status__in=["PaymentApproved", "InProgress", "Completed", "Reviewed"],
            )
        if queue == "radiology":
            qs = qs.filter(
                order_type="radiology",
                status__in=["PaymentApproved", "InProgress", "Completed", "Reviewed"],
            )
        if queue == "results":
            # Doctor inbox: completed / reviewed diagnostic reports.
            qs = qs.filter(
                order_type__in=["lab", "radiology"],
                status__in=["Completed", "Reviewed"],
            )
        if queue == "pharmacy":
            qs = qs.filter(
                order_type="prescription",
                fulfillment=ClinicalOrder.FULFILLMENT_CLINIC,
                status__in=["PaymentApproved", "InProgress"],
            )
        qs = (
            qs.select_related(
                "encounter", "encounter__patient", "billable", "billable__service", "medicine"
            )
            .select_related("radiology_report")
            .prefetch_related("result_values", "billable__service__parameters")
        )
        if queue in ("lab", "radiology", "pharmacy"):
            # Work queues: Very high, then High, then Normal; the first request in a priority is done first.
            return qs.annotate(prio=priority_rank("encounter__priority")).order_by("prio", "created_at")
        return qs.order_by("-updated_at")

    def _parse_rx_amount(self, medicine):
        """Read and check `quantity` / `sale_unit` of a prescription for an inventory medicine."""
        raw_qty = self.request.data.get("quantity", 1)
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            raise ValidationError({"quantity": ["Enter a whole number."]})
        if qty < 1 or qty > 100000:
            raise ValidationError({"quantity": ["Quantity must be at least 1."]})
        unit = (self.request.data.get("sale_unit") or stock.PACK).strip().lower()
        if unit not in stock.SALE_UNITS:
            raise ValidationError({"sale_unit": ["Choose pack or loose."]})
        if unit == stock.LOOSE and not stock.is_splittable(medicine):
            raise ValidationError({"sale_unit": [f"{medicine.name} is not sold loose."]})
        # Stock is not checked here: whatever the clinic cannot supply above its low-stock alert level goes to an
        # external pharmacy order (see perform_create).
        return qty, unit

    def _stock_split(self, medicine, qty, unit):
        """(from the clinic pharmacy, to an external pharmacy) for an order of `qty` in `unit`."""
        clinic_part = min(qty, stock.clinic_can_supply(medicine, unit))
        return clinic_part, qty - clinic_part

    @action(detail=False, methods=["get"])
    def stock_check(self, request):
        """What the doctor should be told before ordering this much of a medicine."""
        medicine = Medicine.objects.filter(
            pk=request.query_params.get("medicine") or 0,
            clinic_tin=self.get_clinic_tin(),
            branch_name__iexact=self.get_branch_name(),
        ).first()
        if medicine is None:
            raise ValidationError({"medicine": ["Medicine not found."]})
        unit = (request.query_params.get("sale_unit") or stock.PACK).lower()
        if unit not in stock.SALE_UNITS or (unit == stock.LOOSE and not stock.is_splittable(medicine)):
            unit = stock.PACK
        try:
            qty = max(1, int(request.query_params.get("quantity") or 1))
        except ValueError:
            qty = 1
        clinic_part, external_part = self._stock_split(medicine, qty, unit)
        reserve = stock.reserve(medicine, unit)
        available = stock.capacity(medicine, unit)
        already_low = available <= reserve
        word = stock.loose_label(medicine) if unit == stock.LOOSE else stock.pack_label(medicine)
        if already_low:
            message = (
                f"{medicine.name} is at or below its low-stock alert level ({stock.stock_text(medicine)} left). "
                f"The whole order of {qty} {stock._plural(word, qty)} goes to an external pharmacy."
            )
        elif external_part:
            message = (
                f"{medicine.name} would pass its low-stock alert level. {clinic_part} {stock._plural(word, clinic_part)} "
                f"come from the clinic pharmacy and {external_part} {stock._plural(word, external_part)} go to an external pharmacy."
            )
        else:
            message = ""
        return Response(
            {
                "stock_text": stock.stock_text(medicine),
                "alert_level": medicine.min_threshold,
                "available": available,
                "clinic_quantity": clinic_part,
                "external_quantity": external_part,
                "low": already_low or external_part > 0,
                "message": message,
            }
        )

    def create(self, request, *args, **kwargs):
        self._stock_split_result = None
        response = super().create(request, *args, **kwargs)
        if self._stock_split_result:
            response.data["stock_split"] = self._stock_split_result
        return response

    EXPIRY_MARGIN_DAYS = 5
    EXPIRY_WARN_DAYS = 10

    def _check_expiry(self, medicine):
        """The doctor can't order expired medicine, or one that runs out of date too close to the end of treatment.

        Returns (treatment end date or None, till medicine ends, warning text).
        """
        data = self.request.data
        till = str(data.get("till_medicine_ends") or "").lower() in {"1", "true", "yes"}
        raw_end = (data.get("duration_end") or "").strip() if isinstance(data.get("duration_end"), str) else data.get("duration_end")
        end = None
        if raw_end:
            try:
                end = datetime.date.fromisoformat(str(raw_end)[:10])
            except ValueError:
                raise ValidationError({"duration_end": ["Pick the end of the treatment on the calendar."]})
            if end < timezone.localdate():
                raise ValidationError({"duration_end": ["The treatment can't end in the past."]})
            till = False
        expires = medicine.expiry_date
        if expires is not None:
            expires = expires.date() if hasattr(expires, "date") else expires
            today = timezone.localdate()
            if expires < today:
                raise ValidationError({"detail": f"{medicine.name} expired on {expires:%d %b %Y}. Choose another batch or medicine."})
            if end is not None:
                gap = (expires - end).days
                if gap < self.EXPIRY_MARGIN_DAYS:
                    raise ValidationError(
                        {"detail": f"{medicine.name} expires on {expires:%d %b %Y}, only {max(gap, 0)} day(s) after the treatment ends. "
                                   f"It must expire at least {self.EXPIRY_MARGIN_DAYS} days after."}
                    )
                if gap <= self.EXPIRY_WARN_DAYS:
                    return end, False, (
                        f"{medicine.name} expires on {expires:%d %b %Y}, {gap} days after the treatment ends. "
                        "Warn the patient to finish it on time."
                    )
            elif till and (expires - today).days <= self.EXPIRY_WARN_DAYS:
                return None, True, (
                    f"{medicine.name} expires on {expires:%d %b %Y}. Warn the patient to use it before then."
                )
        return end, till, ""

    def _warn_if_repeated(self, encounter, data, service_id, medicine_id, medicine_name):
        """The doctor already sent this request for this patient in this visit: ask before sending it again."""
        order_type = data.get("order_type")
        repeats = []
        for other in encounter.orders.filter(order_type=order_type).select_related("billable", "medicine"):
            if order_type == "prescription":
                same = (medicine_id not in (None, "") and str(other.medicine_id) == str(medicine_id)) or (
                    bool(medicine_name) and text_key(medicine_name) in text_key(other.details)
                )
            else:
                same = (
                    service_id not in (None, "")
                    and other.billable_id
                    and str(other.billable.service_id) == str(service_id)
                ) or (service_id in (None, "") and text_key(other.details) == text_key(data.get("details")))
            if same:
                repeats.append(other)
        if repeats:
            kind = {"lab": "lab", "radiology": "radiology", "prescription": "medicine"}.get(order_type, "")
            warn_unless_confirmed(
                self.request,
                f"{encounter.patient.full_name} already has this {kind} request in this visit "
                f"(status: {repeats[0].status}). Send it again?",
                [{"id": o.id, "status": o.status, "details": (o.details or "")[:120]} for o in repeats],
            )

    def perform_create(self, serializer):
        encounter = serializer.validated_data["encounter"]
        branch = self.get_branch_name()
        if encounter.clinic_tin != self.get_clinic_tin():
            raise PermissionError("Cross-tenant order blocked.")
        if (encounter.branch_name or "").strip().lower() != branch.lower():
            raise PermissionError("Cross-branch order blocked.")
        service_id = self.request.data.get("service")
        medicine_id = self.request.data.get("medicine")
        medicine_name = (self.request.data.get("medicine_name") or "").strip()
        fulfillment = (self.request.data.get("fulfillment") or "").strip()
        self._warn_if_repeated(encounter, serializer.validated_data, service_id, medicine_id, medicine_name)
        description = serializer.validated_data.get("details") or "Clinical order"
        price = Decimal("0")
        dept = "Laboratory"
        service = None
        medicine = None
        rx_quantity, rx_sale_unit = 1, stock.PACK
        if medicine_id not in (None, ""):
            try:
                medicine = Medicine.objects.filter(
                    pk=int(medicine_id),
                    clinic_tin=self.get_clinic_tin(),
                    branch_name__iexact=branch,
                ).first()
            except (TypeError, ValueError):
                medicine = None
            if medicine and not description:
                description = medicine.name
        clinic_part = external_part = 0
        if medicine and serializer.validated_data.get("order_type") == "prescription" and service_id in (None, ""):
            rx_quantity, rx_sale_unit = self._parse_rx_amount(medicine)
            clinic_part, external_part = self._stock_split(medicine, rx_quantity, rx_sale_unit)
        # In-stock medicine → clinic pharmacy; free-text name → outside pharmacy print.
        if serializer.validated_data.get("order_type") == "prescription":
            if medicine:
                # Nothing above the low-stock alert level: the whole order goes to an external pharmacy.
                fulfillment = (
                    ClinicalOrder.FULFILLMENT_EXTERNAL
                    if service_id in (None, "") and clinic_part == 0
                    else ClinicalOrder.FULFILLMENT_CLINIC
                )
            elif medicine_name:
                fulfillment = ClinicalOrder.FULFILLMENT_EXTERNAL
                if medicine_name.lower() not in description.lower():
                    description = f"{medicine_name} · {description}" if description else medicine_name
            elif fulfillment not in {
                ClinicalOrder.FULFILLMENT_CLINIC,
                ClinicalOrder.FULFILLMENT_EXTERNAL,
            }:
                fulfillment = ClinicalOrder.FULFILLMENT_CLINIC
        elif fulfillment not in {
            ClinicalOrder.FULFILLMENT_CLINIC,
            ClinicalOrder.FULFILLMENT_EXTERNAL,
        }:
            fulfillment = ClinicalOrder.FULFILLMENT_CLINIC
        if service_id not in (None, ""):
            try:
                service = BillableService.objects.filter(
                    pk=int(service_id),
                    clinic_tin=self.get_clinic_tin(),
                    branch_name__iexact=branch,
                ).first()
            except (TypeError, ValueError):
                service = None
            if service:
                price = service.unit_price
                dept = service.department
                description = (service.description or service.name).strip()[:255]
        elif medicine and serializer.validated_data.get("order_type") == "prescription":
            price = stock.unit_price_for(medicine, rx_sale_unit)
            dept = "Pharmacy"
        elif serializer.validated_data.get("order_type") == "radiology":
            dept = "Radiology"
        duration_end, till_ends, expiry_warning = None, False, ""
        if medicine and serializer.validated_data.get("order_type") == "prescription":
            duration_end, till_ends, expiry_warning = self._check_expiry(medicine)
        quantity = max(1, int(getattr(service, "default_quantity", None) or 1)) if service else 1
        if medicine and serializer.validated_data.get("order_type") == "prescription":
            quantity = clinic_part or rx_quantity  # billed per pack, or per loose unit when sold loose
        payment_status = "AwaitingPayment"
        order_status = "AwaitingPayment"
        # External print prescriptions do not enter the pharmacy queue / payment gate.
        if (
            serializer.validated_data.get("order_type") == "prescription"
            and fulfillment == ClinicalOrder.FULFILLMENT_EXTERNAL
        ):
            payment_status = "PaymentApproved"
            order_status = "PaymentApproved"
            price = Decimal("0")
        elif service and not service.requires_payment_before_work:
            payment_status = "PaymentApproved"
            order_status = "PaymentApproved"
        if service and encounter.referral_from_id and not service.includes_referral_patients:
            # A referred patient comes with results from the other branch: nothing to pay for this service.
            price = Decimal("0")
            payment_status = "PaymentApproved"
            order_status = "PaymentApproved"
            description = f"{description} (referral patient)"[:255]
        billable = None
        if not (
            serializer.validated_data.get("order_type") == "prescription"
            and fulfillment == ClinicalOrder.FULFILLMENT_EXTERNAL
        ):
            billable = BillableItem.objects.create(
                encounter=encounter,
                service=service,
                description=description[:255],
                department=dept,
                unit_price=price,
                quantity=quantity,
                payment_status=payment_status,
            )
        order = serializer.save(
            created_by=self.request.user,
            status=order_status,
            billable=billable,
            medicine=medicine,
            quantity=clinic_part or rx_quantity,
            sale_unit=rx_sale_unit,
            fulfillment=fulfillment
            if serializer.validated_data.get("order_type") == "prescription"
            else ClinicalOrder.FULFILLMENT_CLINIC,
            duration_end=duration_end,
            till_medicine_ends=till_ends,
            expiry_warning=expiry_warning,
        )
        if medicine and clinic_part and external_part:
            # The part the clinic can not supply without going below its alert level becomes an external order.
            word = stock.loose_label(medicine) if rx_sale_unit == stock.LOOSE else stock.pack_label(medicine)
            extra = ClinicalOrder.objects.create(
                encounter=encounter,
                order_type="prescription",
                status="PaymentApproved",
                details=f"{medicine.name} · {external_part} {stock._plural(word, external_part)} "
                f"(above the clinic's low-stock level) · {order.details or ''}".strip(" ·"),
                fulfillment=ClinicalOrder.FULFILLMENT_EXTERNAL,
                medicine=medicine,
                quantity=external_part,
                sale_unit=rx_sale_unit,
                created_by=self.request.user,
            )
            self._stock_split_result = {
                "clinic_quantity": clinic_part,
                "external_quantity": external_part,
                "external_order": extra.id,
            }
        elif medicine and external_part and not clinic_part:
            self._stock_split_result = {"clinic_quantity": 0, "external_quantity": external_part, "external_order": order.id}

    @action(detail=True, methods=["post"])
    def start(self, request, pk=None):
        order = self.get_object()
        if order.status != "PaymentApproved":
            return Response({"detail": "Payment must be approved first."}, status=400)
        order.status = "InProgress"
        order.save(update_fields=["status"])
        return Response(ClinicalOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        order = self.get_object()
        if order.status not in {"PaymentApproved", "InProgress"}:
            return Response({"detail": "Order is not ready to complete."}, status=400)
        if order.order_type == "radiology":
            return self._complete_radiology(request, order)
        params = []
        if order.order_type == "lab" and order.billable_id and order.billable.service_id:
            params = list(order.billable.service.parameters.all())
        if params:
            # Structured entry: every parameter needs a valid value.
            raw_values = request.data.get("values") or {}
            if not isinstance(raw_values, dict):
                return Response({"detail": "values must be an object keyed by parameter id."}, status=400)
            rows, errors = [], []
            for param in params:
                try:
                    value, flag = param.evaluate(raw_values.get(str(param.id)))
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                rows.append(
                    OrderResultValue(
                        order=order,
                        parameter=param,
                        name=param.name,
                        unit=param.unit,
                        reference_text=param.reference_text,
                        value=value,
                        flag=flag,
                        sort_order=param.sort_order,
                        entered_by=request.user,
                    )
                )
            if errors:
                return Response({"detail": " ".join(errors)}, status=400)
            with transaction.atomic():
                for old_value in OrderResultValue.objects.filter(order=order):
                    old_value.soft_delete()
                # The order came with its result values prefetched; forget that list so the reply shows the new ones.
                getattr(order, "_prefetched_objects_cache", {}).pop("result_values", None)
                OrderResultValue.objects.bulk_create(rows)
                order.result_text = chr(10).join(
                    f"{r.name}: {r.value}{(' ' + r.unit) if r.unit else ''}"
                    + (f" [{r.flag.upper()}]" if r.flag != "normal" else "")
                    for r in rows
                )
                order.status = "Completed"
                order.save(update_fields=["result_text", "status"])
        else:
            text = (request.data.get("result_text") or "").strip()
            if not text and not order.result_text:
                return Response({"detail": "Enter a result before completing."}, status=400)
            order.result_text = text or order.result_text
            order.status = "Completed"
            order.save(update_fields=["result_text", "status"])
        return Response(ClinicalOrderSerializer(order).data)

    def _complete_radiology(self, request, order):
        data = request.data
        findings = (data.get("findings") or "").strip()
        impression = (data.get("impression") or "").strip()
        tag = (data.get("tag") or "").strip().lower()
        if not findings or not impression:
            return Response({"detail": "Findings and impression are required."}, status=400)
        if tag not in {"normal", "abnormal", "critical"}:
            return Response({"detail": "Choose Normal, Abnormal or Critical."}, status=400)
        attachments = data.get("attachments") or []
        if not isinstance(attachments, list) or len(attachments) > 10:
            return Response({"detail": "Attach at most 10 images."}, status=400)
        clean = []
        for item in attachments:
            url = (item.get("url") if isinstance(item, dict) else "") or ""
            if not url.startswith("https://"):
                return Response({"detail": "Invalid attachment URL."}, status=400)
            clean.append({"url": url[:500], "name": str(item.get("name") or "")[:120]})
        technique = (data.get("technique") or "").strip()
        with transaction.atomic():
            RadiologyReport.objects.update_or_create(
                order=order,
                defaults=dict(
                    technique=technique, findings=findings, impression=impression,
                    tag=tag, attachments=clean, reported_by=request.user,
                ),
            )
            order.result_text = (
                f"[{tag.upper()}] Impression: {impression}" + chr(10) + f"Findings: {findings}"
            )
            order.status = "Completed"
            order.save(update_fields=["result_text", "status"])
        return Response(ClinicalOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def review(self, request, pk=None):
        order = self.get_object()
        order.status = "Reviewed"
        order.save(update_fields=["status"])
        return Response(ClinicalOrderSerializer(order).data)

    @action(detail=True, methods=["post"])
    def dispense(self, request, pk=None):
        """Sell and stock out the medicine the doctor ordered, exactly as ordered and billed.

        The pharmacist does not choose a medicine, quantity or unit: the order already says
        which medicine, how many, and whether they are whole packs or loose units.
        Anything sent in the request body is ignored.
        """
        if role_of(request.user) not in {"pharmacist", "manager"}:
            return Response({"detail": "Forbidden."}, status=403)
        order = self.get_object()
        if order.order_type != "prescription":
            return Response({"detail": "Not a prescription."}, status=400)
        if order.status not in {"PaymentApproved", "InProgress"}:
            return Response({"detail": "Rx is not payment-approved."}, status=400)
        if not order.medicine_id:
            return Response(
                {
                    "detail": "This prescription is not linked to a medicine in the clinic "
                    "inventory. Ask the doctor to re-issue it from the medicine list."
                },
                status=400,
            )
        qty = max(1, int(order.quantity))
        with transaction.atomic():
            # Lock the stock row so two dispenses cannot both pass the stock check.
            medicine = Medicine.objects.select_for_update().filter(
                pk=order.medicine_id,
                clinic_tin=self.get_clinic_tin(),
                branch_name__iexact=self.get_branch_name(),
            ).first()
            if not medicine:
                return Response({"detail": "Medicine not found in this branch."}, status=404)
            try:
                stock.deduct(medicine, qty, order.sale_unit)
            except stock.StockError as exc:
                return Response({"detail": exc.message}, status=400)
            medicine.save(update_fields=stock.STOCK_FIELDS)
            ledger.record_movement(medicine, "dispense", f"order:{order.pk}", request.user)
            order.status = "Dispensed"
            order.save(update_fields=["status"])
        return Response(ClinicalOrderSerializer(order).data)


class ChartViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = DoctorChartSerializer

    def get_queryset(self):
        return DoctorChart.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        )

    def create(self, request, *args, **kwargs):
        encounter = Encounter.objects.filter(
            pk=request.data.get("encounter"),
            clinic_tin=self.get_clinic_tin(),
            branch_name__iexact=self.get_branch_name(),
        ).first()
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        # One chart per patient per visit: a second save edits the first.
        chart, _ = DoctorChart.objects.update_or_create(
            encounter=encounter,
            defaults={
                "chief_complaint": request.data.get("chief_complaint", ""),
                "examination": request.data.get("examination", ""),
                "diagnosis": request.data.get("diagnosis", ""),
                "clinical_notes": request.data.get("clinical_notes", ""),
                "treatment_plan": request.data.get("treatment_plan", ""),
            },
        )
        # Allergies are recorded by the doctor and kept on the patient, so every later visit and the pharmacy see them.
        if "allergies" in request.data:
            allergies = str(request.data.get("allergies") or "").strip()
            patient = encounter.patient
            if (patient.allergies or "") != allergies:
                patient.allergies = allergies
                patient.save(update_fields=["allergies", "updated_at"])
        return Response(DoctorChartSerializer(chart).data, status=200)


class NurseNoteViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = NurseNoteSerializer

    def get_queryset(self):
        return NurseNote.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        )

    @action(detail=False, methods=["get"], url_path="vitals-config")
    def vitals_config(self, request):
        """The assessment types, the stages, and the vitals asked for each pair."""
        return Response(vitals_mod.config())

    def create(self, request, *args, **kwargs):
        """One record per patient, visit, assessment type and stage: a second save edits the first.

        Under observation is the exception, because several timed readings are expected there.
        """
        assessment = request.data.get("assessment_type") or vitals_mod.DEFAULT_ASSESSMENT
        stage = request.data.get("stage") or vitals_mod.DEFAULT_STAGE
        if stage not in vitals_mod.REPEATING_STAGES and request.data.get("encounter"):
            existing = (
                self.get_queryset()
                .filter(encounter_id=request.data.get("encounter"), assessment_type=assessment, stage=stage)
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                serializer = self.get_serializer(existing, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                data = dict(serializer.data)
                data["edited_existing"] = True
                return Response(data, status=status.HTTP_200_OK)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


def priority_rank(path):
    """Very high first, then High, then Normal (used to sort every board and queue)."""
    return Case(
        When(**{path: "very_high"}, then=Value(0)),
        When(**{path: "high"}, then=Value(1)),
        default=Value(2),
        output_field=IntegerField(),
    )


def pick_rule(view, value, kind):
    """A discount or refund rule chosen at registration: it must be one of this branch's rules of that kind."""
    if value in (None, "", 0, "0"):
        return None
    rule = PaymentRule.objects.filter(
        pk=value, kind=kind, clinic_tin=view.get_clinic_tin(), branch_name__iexact=view.get_branch_name()
    ).first()
    if rule is None:
        raise ValidationError({f"{kind}_rule": [f"That {kind} rule does not exist."]})
    return rule


class AppointmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        qs = scope_branch(
            Appointment.objects.filter(clinic_tin=self.get_clinic_tin()).select_related(
                "patient", "merged_with__patient", "discount_rule", "refund_rule"
            ),
            self.request.user,
        )
        params = self.request.query_params
        now = timezone.now()
        if params.get("today") == "1":
            qs = qs.filter(scheduled_at__date=timezone.localdate())
        if params.get("kind") in ("registry", "follow_up"):
            qs = qs.filter(kind=params["kind"])
        if params.get("status"):
            qs = qs.filter(status__in=params["status"].split(","))
        window = params.get("range")
        if window == "today":
            qs = qs.filter(scheduled_at__date=timezone.localdate())
        elif window == "upcoming":
            qs = qs.filter(status__in=Appointment.ACTIVE, scheduled_at__gte=now)
        elif window == "history":
            qs = qs.exclude(status__in=Appointment.ACTIVE, scheduled_at__gte=now)
        order = "-scheduled_at" if window == "history" else "scheduled_at"
        return qs.order_by(order)

    # --- rules that keep the schedule sensible -------------------------------------------------

    def _refuse_second_appointment(self, patient_id, exclude_pk=None):
        """Until a patient reaches the first appointment's date and time they can't get a second: edit the first."""
        pending = (
            Appointment.objects.filter(
                clinic_tin=self.get_clinic_tin(),
                branch_name__iexact=self.get_branch_name(),
                patient_id=patient_id,
                status__in=Appointment.ACTIVE,
                scheduled_at__gte=timezone.now(),
            )
            .exclude(pk=exclude_pk)
            .select_related("patient")
            .order_by("scheduled_at")
            .first()
        )
        if pending is not None:
            when = timezone.localtime(pending.scheduled_at).strftime("%d %b %Y %H:%M")
            raise Conflict(
                f"{pending.patient.full_name} already has an appointment on {when}. "
                "Edit that one instead of booking a second.",
                "open_appointment",
                appointment_id=pending.id,
                scheduled_at=pending.scheduled_at.isoformat(),
                kind=pending.kind,
            )

    def _overlap_choice(self, when, exclude_pk=None):
        """Look for a saved appointment at the exact same date and time.

        With no answer yet this stops with a 409 listing them. The user then resends with `overlap` set to
        `merge` (keep both at that time, linked) or `cancel_saved`; changing the day or time needs no flag.
        Returns (merge_target, appointments_to_cancel).
        """
        start = when.replace(second=0, microsecond=0)
        clashes = list(
            Appointment.objects.filter(
                clinic_tin=self.get_clinic_tin(),
                branch_name__iexact=self.get_branch_name(),
                status__in=Appointment.ACTIVE,
                scheduled_at__gte=start,
                scheduled_at__lt=start + timedelta(minutes=1),
            )
            .exclude(pk=exclude_pk)
            .select_related("patient")
        )
        if not clashes:
            return None, []
        choice = str(self.request.data.get("overlap") or "").strip().lower()
        if choice == "merge":
            return clashes[0], []
        if choice == "cancel_saved":
            return None, clashes
        who = ", ".join(c.patient.full_name for c in clashes)
        raise Conflict(
            f"{who} already {'has' if len(clashes) == 1 else 'have'} an appointment at exactly this date and time.",
            "appointment_overlap",
            overlaps=[
                {"id": c.id, "patient_name": c.patient.full_name, "kind": c.kind, "scheduled_at": c.scheduled_at.isoformat()}
                for c in clashes
            ],
        )

    @staticmethod
    def _apply_overlap(appointment, merge_target, to_cancel):
        if merge_target is not None:
            appointment.merged_with = merge_target
            appointment.save(update_fields=["merged_with", "updated_at"])
        for saved in to_cancel:
            saved.status = "cancelled"
            saved.save(update_fields=["status", "updated_at"])

    # --- create / register -----------------------------------------------------------------------

    def create(self, request, *args, **kwargs):
        """A doctor's follow-up for the same patient in the same visit edits the one already booked.

        Only while that earlier follow-up has not arrived yet; after its time has passed a new one is a new booking.
        """
        kind = request.data.get("kind") or Appointment.KIND_REGISTRY
        number = (request.data.get("encounter_number") or "").strip()
        patient_id = request.data.get("patient")
        if kind == Appointment.KIND_FOLLOW_UP and number and patient_id:
            existing = (
                self.get_queryset()
                .filter(
                    patient_id=patient_id,
                    kind=Appointment.KIND_FOLLOW_UP,
                    encounter_number=number,
                    status__in=Appointment.ACTIVE,
                    scheduled_at__gte=timezone.now(),
                )
                .order_by("scheduled_at")
                .first()
            )
            if existing is not None:
                serializer = self.get_serializer(
                    existing,
                    data={"scheduled_at": request.data.get("scheduled_at"), "reason": request.data.get("reason", existing.reason)},
                    partial=True,
                )
                serializer.is_valid(raise_exception=True)
                merge, cancel = self._overlap_choice(serializer.validated_data["scheduled_at"], exclude_pk=existing.pk)
                with transaction.atomic():
                    serializer.save()
                    self._apply_overlap(existing, merge, cancel)
                data = dict(serializer.data)
                data["edited_existing"] = True
                return Response(data, status=status.HTTP_200_OK)
        if patient_id:
            self._refuse_second_appointment(patient_id)
        return super().create(request, *args, **kwargs)

    def perform_create(self, serializer):
        merge, cancel = self._overlap_choice(serializer.validated_data["scheduled_at"])
        discount = pick_rule(self, self.request.data.get("discount_rule"), "discount")
        refund = pick_rule(self, self.request.data.get("refund_rule"), "refund")
        with transaction.atomic():
            appointment = serializer.save(
                clinic_tin=self.get_clinic_tin(),
                branch_name=self.get_branch_name(),
                created_by=self.request.user,
                discount_rule=discount,
                refund_rule=refund,
                status="scheduled",
            )
            self._apply_overlap(appointment, merge, cancel)

    @action(detail=False, methods=["post"])
    def register(self, request):
        """Appointment registration: everything New patient registration asks for, plus the date and time."""
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can register appointments."}, status=403)
        tin, branch = self.get_clinic_tin(), self.get_branch_name()
        patient_ser = PatientSerializer(
            data={k: request.data.get(k) for k in ("full_name", "age", "gender", "phone", "address") if k in request.data}
        )
        patient_ser.is_valid(raise_exception=True)
        existing = PatientViewSet._already_registered(None, patient_ser.validated_data, tin, branch)
        if existing is not None:
            raise Conflict(
                f"{existing.full_name} ({existing.mrn}) is already registered. "
                "Use their record to book the appointment, or edit the one they already have.",
                "patient_exists",
                patient_id=existing.id,
                mrn=existing.mrn,
                open_encounter=None,
            )
        appt_ser = AppointmentSerializer(
            data={"patient": 0, "scheduled_at": request.data.get("scheduled_at"), "reason": request.data.get("reason", "")}
        )
        when_field = appt_ser.fields["scheduled_at"]
        try:
            when = when_field.run_validation(request.data.get("scheduled_at"))
        except Exception:
            raise ValidationError({"scheduled_at": ["Pick the appointment date and time."]})
        if when < timezone.now():
            raise ValidationError({"scheduled_at": ["The appointment must be in the future."]})
        priority = request.data.get("priority") or "normal"
        if priority not in ("very_high", "high", "normal"):
            raise ValidationError({"priority": ["Choose Very high, High or Normal."]})
        discount = pick_rule(self, request.data.get("discount_rule"), "discount")
        refund = pick_rule(self, request.data.get("refund_rule"), "refund")
        merge, cancel = self._overlap_choice(when)
        with transaction.atomic():
            count = Patient.objects.filter(clinic_tin=tin, branch_name__iexact=branch).count() + 1
            patient = patient_ser.save(clinic_tin=tin, branch_name=branch, mrn=f"MRN-{count:05d}")
            appointment = Appointment.objects.create(
                clinic_tin=tin,
                branch_name=branch,
                patient=patient,
                scheduled_at=when,
                reason=str(request.data.get("reason") or "").strip()[:255],
                kind=Appointment.KIND_REGISTRY,
                created_by=request.user,
                priority=priority,
                discount_rule=discount,
                refund_rule=refund,
            )
            self._apply_overlap(appointment, merge, cancel)
        return Response(AppointmentSerializer(appointment).data, status=201)

    # --- change an appointment -------------------------------------------------------------------

    def _must_be_active(self, appointment):
        if appointment.status not in Appointment.ACTIVE:
            raise ValidationError({"detail": f"This appointment is already {appointment.get_status_display().lower()}."})

    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, pk=None):
        """Mark an appointment Done, No-show or Cancelled. The status stays on the record."""
        appointment = self.get_object()
        new_status = str(request.data.get("status") or "")
        if new_status not in ("done", "no_show", "cancelled"):
            raise ValidationError({"detail": "Choose done, no_show or cancelled."})
        self._must_be_active(appointment)
        appointment.status = new_status
        appointment.save(update_fields=["status", "updated_at"])
        return Response(AppointmentSerializer(appointment).data)

    @action(detail=True, methods=["post"])
    def reschedule(self, request, pk=None):
        appointment = self.get_object()
        self._must_be_active(appointment)
        try:
            when = AppointmentSerializer().fields["scheduled_at"].run_validation(request.data.get("scheduled_at"))
        except Exception:
            raise ValidationError({"scheduled_at": ["Pick the new date and time."]})
        if when < timezone.now():
            raise ValidationError({"scheduled_at": ["The new time must be in the future."]})
        if when.replace(second=0, microsecond=0) == appointment.scheduled_at.replace(second=0, microsecond=0):
            raise ValidationError({"scheduled_at": ["That is the same date and time. Pick a different one."]})
        merge, cancel = self._overlap_choice(when, exclude_pk=appointment.pk)
        with transaction.atomic():
            appointment.scheduled_at = when
            appointment.status = "rescheduled"
            appointment.rescheduled_count += 1
            appointment.merged_with = None
            appointment.save(update_fields=["scheduled_at", "status", "rescheduled_count", "merged_with", "updated_at"])
            self._apply_overlap(appointment, merge, cancel)
        return Response(AppointmentSerializer(appointment).data)


class ManagerWritesMixin:
    """Everyone in the clinic may read; only the manager may create, change or delete."""

    manager_write_actions = {"create", "update", "partial_update", "destroy", "batch"}

    def check_permissions(self, request):
        super().check_permissions(request)
        if self.action in self.manager_write_actions and role_of(request.user) != "manager":
            raise PermissionDenied("Only the clinic manager can change this catalog.")


class PaymentRuleViewSet(ManagerWritesMixin, TenantScopedMixin, viewsets.ModelViewSet):
    """The manager's discount and refund rules. Everyone in the clinic may read them (reception picks one)."""

    serializer_class = PaymentRuleSerializer

    def get_queryset(self):
        qs = PaymentRule.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        )
        kind = self.request.query_params.get("kind")
        return qs.filter(kind=kind) if kind in ("discount", "refund") else qs

    def _check(self, data, exclude_pk=None, kind=None):
        kind = data.get("kind", kind)
        name = (data.get("name") or "").strip()
        pool = PaymentRule.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name(), kind=kind
        )
        if exclude_pk:
            pool = pool.exclude(pk=exclude_pk)
        if any(text_key(r.name) == text_key(name) for r in pool.filter(name__iexact=name)):
            raise ValidationError({"name": [f"A {kind} rule named {name} already exists."]})
        if not data.get("all_services"):
            ids = data.get("service_ids") or []
            known = set(
                BillableService.objects.filter(
                    clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name(), pk__in=ids
                ).values_list("pk", flat=True)
            )
            if not ids or set(ids) - known:
                raise ValidationError({"service_ids": ["Choose All services or at least one existing service."]})

    def perform_create(self, serializer):
        self._check(serializer.validated_data)
        serializer.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name())

    def perform_update(self, serializer):
        merged = {f: getattr(serializer.instance, f) for f in ("name", "all_services", "service_ids")}
        merged.update(serializer.validated_data)
        self._check(merged, exclude_pk=serializer.instance.pk, kind=serializer.instance.kind)
        serializer.save()

    @action(detail=False, methods=["post"])
    def batch(self, request):
        """Several rules at once: all are saved, or none are."""
        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError({"detail": "Add at least one rule."})
        if len(items) > 100:
            raise ValidationError({"detail": "At most 100 rules at a time."})
        sers = [PaymentRuleSerializer(data=i, context=self.get_serializer_context()) for i in items]
        errors, seen = [], set()
        for index, ser in enumerate(sers, start=1):
            if not ser.is_valid():
                name = (items[index - 1] or {}).get("name") or f"Line {index}"
                errors.append({"line": index, "name": name, "errors": ser.errors})
        if errors:
            return Response(
                {"detail": f"{len(errors)} rule(s) need fixing. Nothing was saved.", "errors": errors}, status=400
            )
        for index, ser in enumerate(sers, start=1):
            try:
                self._check(ser.validated_data)
            except ValidationError as exc:
                msg = " ".join(str(m) for v in exc.detail.values() for m in (v if isinstance(v, list) else [v]))
                raise ValidationError({"detail": f"Line {index}: {msg} Nothing was saved."})
            key = (ser.validated_data["kind"], text_key(ser.validated_data["name"]))
            if key in seen:
                raise ValidationError({"detail": f"Line {index} repeats an earlier rule name. Nothing was saved."})
            seen.add(key)
        with transaction.atomic():
            saved = [s.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name()) for s in sers]
        return Response(PaymentRuleSerializer(saved, many=True).data, status=201)


class SupplierViewSet(ManagerWritesMixin, TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = SupplierSerializer

    def get_queryset(self):
        return scope_branch(
            Supplier.objects.filter(clinic_tin=self.get_clinic_tin()), self.request.user
        )

    def perform_create(self, serializer):
        name = serializer.validated_data["supplier_name"]
        tin, branch = self.get_clinic_tin(), self.get_branch_name()
        if Supplier.objects.filter(
            clinic_tin=tin, branch_name__iexact=branch, supplier_name__iexact=name
        ).exists():
            raise ValidationError({"supplier_name": ["A supplier with this name already exists."]})
        serializer.save(clinic_tin=tin, branch_name=branch)

    def perform_update(self, serializer):
        name = serializer.validated_data.get("supplier_name")
        if name and Supplier.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name(), supplier_name__iexact=name
        ).exclude(pk=serializer.instance.pk).exists():
            raise ValidationError({"supplier_name": ["A supplier with this name already exists."]})
        serializer.save()

    def perform_destroy(self, instance):
        count = instance.medicines.count()
        if count:
            raise ValidationError(
                {"detail": f"{instance.supplier_name} supplies {count} medicine line(s). Remove or reassign those first."}
            )
        instance.soft_delete()


class MedicineViewSet(ManagerWritesMixin, TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = MedicineSerializer

    def get_queryset(self):
        return scope_branch(
            Medicine.objects.filter(clinic_tin=self.get_clinic_tin()).select_related("supplier"),
            self.request.user,
        )

    IDENTITY = ("name", "supplier", "batch_number", "expiry_date", "unit_of_measure", "unit_price", "cost", "pack_size", "on_hand")

    @staticmethod
    def _norm(value):
        if hasattr(value, "pk"):
            return value.pk
        return text_key(value) if isinstance(value, str) else str(value)

    def _same_name(self, name):
        tin, branch = self.get_clinic_tin(), self.get_branch_name()
        return Medicine.objects.filter(clinic_tin=tin, branch_name__iexact=branch, name__iexact=(name or "").strip())

    def _is_exact_copy(self, other, data):
        for field in self.IDENTITY:
            if field not in data:
                continue
            if self._norm(data[field]) != self._norm(getattr(other, field)):
                return False
        return True

    def _check_new(self, data):
        """Refuse an exact copy of a stock line; return the existing entries that only share the name."""
        similar = []
        for other in self._same_name(data.get("name")):
            if self._is_exact_copy(other, data):
                raise ValidationError(
                    {"detail": f"{other.name} (batch {other.batch_number or '-'}) is already registered exactly like this."}
                )
            similar.append(other)
        return similar

    @staticmethod
    def _similar_message(name, count):
        return (
            f"{name} is already in the inventory ({count} {'entry' if count == 1 else 'entries'}). "
            "This one will be added to the same medicine group."
        )

    def perform_create(self, serializer):
        similar = self._check_new(serializer.validated_data)
        if similar:
            warn_unless_confirmed(
                self.request,
                self._similar_message(serializer.validated_data.get("name"), len(similar)),
                [{"id": m.id, "name": m.name, "batch_number": m.batch_number} for m in similar],
            )
        serializer.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name())

    def perform_destroy(self, instance):
        # Deleting a stock line would erase what is still owed to the supplier.
        owed = (Decimal(instance.received_quantity) * Decimal(instance.cost)) - Decimal(instance.paid_amount)
        if owed > Decimal("0.01"):
            raise ValidationError(
                {
                    "detail": f"{instance.name} still has {owed:.2f} ETB owed to the supplier. "
                    "Record the payment (edit Paid amount) before deleting it."
                }
            )
        instance.soft_delete()

    @action(detail=False, methods=["post"])
    def batch(self, request):
        """Register several medicine lines at once: all are saved, or none are."""
        items = request.data.get("items")
        if not isinstance(items, list) or not items:
            raise ValidationError({"detail": "Add at least one medicine line."})
        if len(items) > 100:
            raise ValidationError({"detail": "At most 100 lines at a time."})
        serializers_ = [MedicineSerializer(data=item, context=self.get_serializer_context()) for item in items]
        errors = []
        for index, ser in enumerate(serializers_, start=1):
            if not ser.is_valid():
                name = (items[index - 1] or {}).get("name") or f"Line {index}"
                errors.append({"line": index, "name": name, "errors": ser.errors})
        if errors:
            return Response(
                {"detail": f"{len(errors)} line(s) need fixing. Nothing was saved.", "errors": errors},
                status=400,
            )
        # Exact copies are refused, whether of stock already saved or of another line in this same batch.
        similar_names, seen = [], []
        for index, ser in enumerate(serializers_, start=1):
            data = ser.validated_data
            try:
                existing = self._check_new(data)
            except ValidationError as exc:
                raise ValidationError({"detail": f"Line {index}: {exc.detail['detail']} Nothing was saved."})
            if existing:
                similar_names.append((data.get("name"), len(existing)))
            for earlier_index, earlier in seen:
                if all(self._norm(earlier.get(f)) == self._norm(data.get(f)) for f in self.IDENTITY if f in data):
                    raise ValidationError(
                        {"detail": f"Line {index} repeats line {earlier_index}. Nothing was saved."}
                    )
            seen.append((index, data))
        if similar_names:
            names = ", ".join(sorted({n for n, _ in similar_names}))
            warn_unless_confirmed(
                self.request,
                f"Already in the inventory: {names}. These lines will be added to the same medicine groups.",
                [{"name": n, "entries": c} for n, c in similar_names],
            )
        tin, branch = self.get_clinic_tin(), self.get_branch_name()
        with transaction.atomic():
            saved = [ser.save(clinic_tin=tin, branch_name=branch) for ser in serializers_]
        return Response(
            MedicineSerializer(saved, many=True, context=self.get_serializer_context()).data,
            status=201,
        )


class MedicineDamageViewSet(ManagerWritesMixin, TenantScopedMixin, viewsets.ModelViewSet):
    """Damaged stock. Recording, changing or deleting an entry moves the medicine's stock to match."""

    serializer_class = MedicineDamageSerializer

    def get_queryset(self):
        qs = scope_branch(
            MedicineDamage.objects.filter(clinic_tin=self.get_clinic_tin()).select_related(
                "medicine", "recorded_by"
            ),
            self.request.user,
        )
        medicine = self.request.query_params.get("medicine")
        if medicine:
            qs = qs.filter(medicine_id=medicine)
        return qs

    def _locked_medicine(self, medicine_id):
        medicine = (
            scope_branch(
                Medicine.objects.select_for_update().filter(clinic_tin=self.get_clinic_tin()),
                self.request.user,
            )
            .filter(pk=medicine_id)
            .first()
        )
        if not medicine:
            raise ValidationError({"medicine": ["Medicine not found."]})
        return medicine

    @staticmethod
    def _take(medicine, quantity, sale_unit):
        try:
            stock.deduct(medicine, quantity, sale_unit)
        except stock.StockError as exc:
            raise ValidationError({"quantity": [exc.message]})
        medicine.save(update_fields=stock.STOCK_FIELDS)

    def perform_create(self, serializer):
        data = serializer.validated_data
        with transaction.atomic():
            medicine = self._locked_medicine(data["medicine"].pk)
            self._take(medicine, data["quantity"], data.get("sale_unit", "pack"))
            entry = serializer.save(
                clinic_tin=medicine.clinic_tin,
                branch_name=medicine.branch_name,
                medicine=medicine,
                recorded_by=self.request.user,
            )
            ledger.record_movement(medicine, "damage", f"damage:{entry.pk}", self.request.user)

    def perform_update(self, serializer):
        old = serializer.instance
        data = serializer.validated_data
        with transaction.atomic():
            medicine = self._locked_medicine(old.medicine_id)
            if "medicine" in data and data["medicine"].pk != old.medicine_id:
                raise ValidationError({"medicine": ["A damage entry can't be moved to another medicine."]})
            stock.restock(medicine, old.quantity, old.sale_unit)
            self._take(medicine, data.get("quantity", old.quantity), data.get("sale_unit", old.sale_unit))
            entry = serializer.save(medicine=medicine)
            ledger.record_movement(medicine, "damage", f"damage:{entry.pk}", self.request.user)

    def perform_destroy(self, instance):
        # Removing a wrong entry puts the stock back.
        with transaction.atomic():
            medicine = self._locked_medicine(instance.medicine_id)
            stock.restock(medicine, instance.quantity, instance.sale_unit)
            medicine.save(update_fields=stock.STOCK_FIELDS)
            ledger.record_movement(medicine, "adjust", f"damage-removed:{instance.pk}", self.request.user)
            instance.soft_delete()


class DepartmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        qs = Department.objects.filter(clinic_tin=self.get_clinic_tin())
        # Read-only peek at another branch's departments for in-org referrals.
        if self.action in ("list", "retrieve"):
            branch = (self.request.query_params.get("branch") or "").strip()
            if branch:
                org = org_tin_of(self.request.user)
                dest = ClinicBranch.objects.filter(
                    clinic_tin__iexact=org, name__iexact=branch
                ).first()
                if not dest:
                    return Department.objects.none()
                dest_tin = dest.operational_tin()
                return Department.objects.filter(
                    clinic_tin__iexact=dest_tin,
                    branch_name__iexact=dest.name,
                )
        return scope_branch(qs, self.request.user)

    def _refuse_duplicate(self, name, exclude_pk=None):
        name = (name or "").strip()
        pool = Department.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        )
        if exclude_pk:
            pool = pool.exclude(pk=exclude_pk)
        if any(text_key(d.name) == text_key(name) for d in pool.filter(name__iexact=name)):
            raise ValidationError({"name": [f"The {name} department already exists."]})

    def perform_create(self, serializer):
        self._refuse_duplicate(serializer.validated_data.get("name"))
        serializer.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name())

    def perform_update(self, serializer):
        instance = serializer.instance
        old_name = instance.name
        if "name" in serializer.validated_data:
            self._refuse_duplicate(serializer.validated_data["name"], exclude_pk=instance.pk)
        updated = serializer.save()
        if old_name != updated.name:
            BillableService.objects.filter(
                clinic_tin=updated.clinic_tin,
                branch_name__iexact=updated.branch_name or self.get_branch_name(),
                department=old_name,
            ).update(department=updated.name)

    def perform_destroy(self, instance):
        in_use = BillableService.objects.filter(
            clinic_tin=instance.clinic_tin,
            branch_name__iexact=instance.branch_name or self.get_branch_name(),
            department=instance.name,
        ).exists()
        if in_use:
            from rest_framework.exceptions import ValidationError

            raise ValidationError(
                {"detail": "Remove or reassign billable services using this department first."}
            )
        instance.soft_delete()


class ClinicBranchViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = ClinicBranchSerializer

    def get_queryset(self):
        # Org-wide: needed so doctors can pick a referral destination branch.
        return ClinicBranch.objects.filter(clinic_tin__iexact=org_tin_of(self.request.user))

    def perform_create(self, serializer):
        tin = org_tin_of(self.request.user) or self.get_clinic_tin()
        is_main = bool(serializer.validated_data.get("is_main"))
        if is_main:
            ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).update(is_main=False)
        branch_tin = (serializer.validated_data.get("branch_tin") or "").strip()
        if not branch_tin and not is_main:
            # A branch without a TIN gets its own unique one, never the organization's: it keeps its own
            # billing, staff, mode and sync token. The main branch is the organization's own location.
            from tenants.services import generate_clinic_tin

            branch_tin = generate_clinic_tin()
        elif not branch_tin:
            branch_tin = self.get_clinic_tin()
        branch = serializer.save(clinic_tin=tin, branch_tin=branch_tin)
        if not ClinicBranch.objects.filter(clinic_tin__iexact=tin).exclude(pk=branch.pk).exists():
            if not branch.is_main:
                branch.is_main = True
                branch.save(update_fields=["is_main"])
        elif branch.is_main:
            ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).exclude(
                pk=branch.pk
            ).update(is_main=False)
        from tenants.services import ensure_tenant_account, seed_branch_catalog

        op_tin = branch.operational_tin() or tin
        seed_branch_catalog(clinic_tin=op_tin, branch_name=branch.name)
        ensure_tenant_account(
            clinic_tin=op_tin,
            clinic_name=getattr(self.request.user.profile, "clinic_name", "") or "",
            logo_url=getattr(self.request.user.profile, "logoUrl", "") or "",
            branch_name=branch.name,
            is_main_branch=False,
        )
        return branch

    def perform_update(self, serializer):
        tin = org_tin_of(self.request.user) or self.get_clinic_tin()
        if serializer.validated_data.get("is_main"):
            ClinicBranch.objects.filter(clinic_tin__iexact=tin, is_main=True).exclude(
                pk=serializer.instance.pk
            ).update(is_main=False)
        serializer.save()


class TicketViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EquipmentTicketSerializer

    def get_queryset(self):
        return scope_branch(
            EquipmentTicket.objects.filter(clinic_tin=self.get_clinic_tin()),
            self.request.user,
        )

    def perform_create(self, serializer):
        data = serializer.validated_data
        same = [
            t
            for t in EquipmentTicket.objects.filter(
                clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name(), status="Open"
            )
            if text_key(t.title) == text_key(data.get("title")) and text_key(t.details) == text_key(data.get("details"))
        ]
        if same:
            warn_unless_confirmed(
                self.request,
                "An open equipment request with the same title and details already exists. Send it again?",
                [{"id": t.id, "title": t.title, "created_at": t.created_at} for t in same],
            )
        serializer.save(
            clinic_tin=self.get_clinic_tin(),
            branch_name=self.get_branch_name(),
            created_by=self.request.user,
        )


def org_member_tins(user):
    """Every operational TIN in the clinic's organization (a branch may have its own)."""
    org = org_tin_of(user)
    tins = {org, tin_of(user)}
    for branch in ClinicBranch.objects.filter(clinic_tin=org):
        tins.add(branch.operational_tin())
    return {t for t in tins if t}


class ReferralViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    """Doctor refers; reception approves branch-to-branch referrals and prints; the receiving manager approves."""

    serializer_class = ReferralSerializer
    incoming_actions = {"approve", "reject", "incoming"}

    def get_queryset(self):
        if self.action in self.incoming_actions:
            qs = Referral.objects.filter(
                destination_kind=Referral.KIND_INTERNAL,
                to_branch__iexact=self.get_branch_name(),
                encounter__clinic_tin__in=org_member_tins(self.request.user),
                reception_approved_at__isnull=False,
            ).exclude(encounter__branch_name__iexact=self.get_branch_name())
            return qs.select_related("encounter", "encounter__patient", "created_by", "approved_by").order_by(
                "-created_at"
            )
        qs = Referral.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        ).select_related("encounter", "encounter__patient", "created_by", "approved_by")
        status_filter = (self.request.query_params.get("approval_status") or "").strip()
        if status_filter:
            qs = qs.filter(approval_status=status_filter)
        kind = (self.request.query_params.get("destination_kind") or "").strip()
        if kind:
            qs = qs.filter(destination_kind=kind)
        return qs.order_by("-created_at")

    # --- creating: a repeat to the same destination replaces the earlier one -----------------

    def _same_destination(self, encounter, data):
        kind = data.get("destination_kind") or Referral.KIND_INTERNAL
        pool = encounter.referrals.filter(destination_kind=kind).exclude(
            approval_status=Referral.APPROVAL_DISQUALIFIED
        )
        if kind == Referral.KIND_INTERNAL:
            return pool.filter(
                to_branch__iexact=(data.get("to_branch") or "").strip(),
                to_department__iexact=(data.get("to_department") or "").strip(),
            )
        return pool.filter(
            external_institution__iexact=(data.get("external_institution") or "").strip(),
            to_department__iexact=(data.get("to_department") or "").strip(),
        )

    def perform_create(self, serializer):
        encounter = serializer.validated_data.get("encounter")
        if encounter is None:
            raise ValidationError({"encounter": ["Choose the visit to refer."]})
        if encounter.clinic_tin != self.get_clinic_tin():
            raise PermissionError("Cross-tenant referral blocked.")
        if (encounter.branch_name or "").strip().lower() != self.get_branch_name().lower():
            raise PermissionError("Cross-branch referral blocked.")
        data = serializer.validated_data
        if data.get("destination_kind", Referral.KIND_INTERNAL) == Referral.KIND_INTERNAL:
            target = (data.get("to_branch") or "").strip()
            if target.lower() == self.get_branch_name().lower():
                raise ValidationError({"to_branch": ["Choose another branch. This is already the patient's branch."]})
            if not ClinicBranch.objects.filter(clinic_tin=org_tin_of(self.request.user), name__iexact=target).exists():
                raise ValidationError({"to_branch": ["That branch is not part of this clinic."]})
        earlier = list(self._same_destination(encounter, data))
        disqualify_new = ""
        if earlier:
            warn_unless_confirmed(
                self.request,
                f"{encounter.patient.full_name} was already referred to this destination in this visit. "
                "Sending it again replaces the earlier referral.",
                [{"id": r.id, "status": r.approval_status} for r in earlier],
            )
            if any(r.approval_status == Referral.APPROVAL_APPROVED for r in earlier):
                disqualify_new = "An earlier referral to the same destination was already approved."
        with transaction.atomic():
            if not disqualify_new:
                for old in earlier:
                    old.approval_status = Referral.APPROVAL_DISQUALIFIED
                    old.disqualified_reason = "Replaced by a newer referral to the same destination."
                    old.save(update_fields=["approval_status", "disqualified_reason", "updated_at"])
            extra = {"created_by": self.request.user}
            if disqualify_new:
                extra["disqualified_reason"] = disqualify_new
            referral = serializer.save(**extra)
            if disqualify_new:
                referral.approval_status = Referral.APPROVAL_DISQUALIFIED
                referral.save(update_fields=["approval_status", "updated_at"])

    # --- sending branch: reception ----------------------------------------------------------

    @action(detail=True, methods=["post"], url_path="reception-approve")
    def reception_approve(self, request, pk=None):
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can approve a referral to another branch."}, status=403)
        referral = self.get_object()
        if referral.destination_kind != Referral.KIND_INTERNAL:
            raise ValidationError({"detail": "Outside referrals need no approval. Print it once everything is paid."})
        if referral.approval_status == Referral.APPROVAL_DISQUALIFIED:
            raise ValidationError({"detail": "This referral was disqualified by a newer one."})
        if referral.reception_approved_at is None:
            referral.reception_approved_by = request.user
            referral.reception_approved_at = timezone.now()
            referral.save(update_fields=["reception_approved_by", "reception_approved_at", "updated_at"])
        return Response(ReferralSerializer(referral).data)

    @action(detail=True, methods=["post"], url_path="print")
    def print_referral(self, request, pk=None):
        """Printing hands the referral to the patient, which also checks the patient out."""
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can print a referral."}, status=403)
        referral = self.get_object()
        blocker = ReferralSerializer(referral).data["print_blocker"]
        if blocker:
            raise ValidationError({"detail": blocker})
        encounter = referral.encounter
        with transaction.atomic():
            if referral.printed_at is None:
                referral.printed_at = timezone.now()
                referral.save(update_fields=["printed_at", "updated_at"])
            payload = perform_checkout(encounter, self.get_branch_name())
        return Response({"referral": ReferralSerializer(referral).data, "checkout": payload})

    # --- receiving branch: the manager ------------------------------------------------------

    @action(detail=False, methods=["get"])
    def incoming(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only the manager sees incoming referrals."}, status=403)
        rows = self.get_queryset()[:200]
        return Response(ReferralSerializer(rows, many=True).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """The receiving manager accepts the patient: they become this branch's patient with a new visit."""
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can approve referrals."}, status=403)
        referral = self.get_object()
        if referral.approval_status == Referral.APPROVAL_APPROVED:
            return Response(ReferralSerializer(referral).data)
        if referral.approval_status != Referral.APPROVAL_PENDING:
            raise ValidationError({"detail": f"This referral is {referral.get_approval_status_display().lower()}."})
        tin, branch = self.get_clinic_tin(), self.get_branch_name()
        source = referral.encounter.patient
        data = {"full_name": source.full_name, "age": source.age, "gender": source.gender, "phone": source.phone}
        local = PatientViewSet._already_registered(None, data, tin, branch)
        if local is not None:
            open_visit = local.encounters.exclude(status=Encounter.STATUS_CLOSED).order_by("-opened_at").first()
            if open_visit is not None:
                raise Conflict(
                    f"{local.full_name} still has an open visit here ({open_visit.number}). "
                    "A referred patient with an open visit cannot be approved until they are checked out.",
                    "open_encounter",
                    patient_id=local.id,
                    open_encounter=open_visit.number,
                )
        discount = pick_rule(self, request.data.get("discount_rule"), "discount")
        refund = pick_rule(self, request.data.get("refund_rule"), "refund")
        with transaction.atomic():
            if local is None:
                count = Patient.objects.filter(clinic_tin=tin, branch_name__iexact=branch).count() + 1
                local = Patient.objects.create(
                    clinic_tin=tin, branch_name=branch, mrn=f"MRN-{count:05d}", full_name=source.full_name,
                    age=source.age, gender=source.gender, phone=source.phone, address=source.address,
                )
            visit = open_visit_for(
                local, "returning" if local.encounters.exists() else "new", tin, branch, request.user,
                priority=referral.priority, discount_rule=discount, refund_rule=refund, referral=referral,
            )
            handover = ReferralSerializer(referral).data["handover_text"]
            DoctorChart.objects.create(
                encounter=visit, diagnosis=(referral.diagnosis or "")[:255], clinical_notes=handover
            )
            referral.approval_status = Referral.APPROVAL_APPROVED
            referral.approved_by = request.user
            referral.approved_at = timezone.now()
            referral.save(update_fields=["approval_status", "approved_by", "approved_at", "updated_at"])
        body = ReferralSerializer(referral).data
        body["received_encounter"] = visit.number
        return Response(body)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can reject referrals."}, status=403)
        referral = self.get_object()
        if referral.approval_status not in (Referral.APPROVAL_PENDING, Referral.APPROVAL_REJECTED):
            raise ValidationError({"detail": f"This referral is {referral.get_approval_status_display().lower()}."})
        referral.approval_status = Referral.APPROVAL_REJECTED
        referral.approved_by = request.user
        referral.approved_at = timezone.now()
        referral.save(update_fields=["approval_status", "approved_by", "approved_at", "updated_at"])
        return Response(ReferralSerializer(referral).data)


class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = tin_of(request.user)
        branch = branch_of(request.user)
        today = timezone.localdate()
        encounters = Encounter.objects.filter(clinic_tin=tin, branch_name__iexact=branch)
        return Response(
            {
                "today_encounters": encounters.filter(opened_at__date=today).count(),
                "open_encounters": encounters.exclude(status=Encounter.STATUS_CLOSED).count(),
                "pending_payments": BillableItem.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                )
                .exclude(payment_status="PaymentApproved")
                .exclude(encounter__status=Encounter.STATUS_CLOSED)
                .values("encounter_id")
                .distinct()
                .count(),
                "pending_payment_items": BillableItem.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                )
                .exclude(payment_status="PaymentApproved")
                .exclude(encounter__status=Encounter.STATUS_CLOSED)
                .count(),
                "lab_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    order_type="lab",
                    status="PaymentApproved",
                ).count(),
                "radiology_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    order_type="radiology",
                    status="PaymentApproved",
                ).count(),
                "results_ready": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    order_type__in=["lab", "radiology"],
                    status="Completed",
                ).count(),
                "rx_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    order_type="prescription",
                    status="PaymentApproved",
                ).count(),
                "low_stock": Medicine.objects.filter(
                    clinic_tin=tin, branch_name__iexact=branch, on_hand__lte=F("min_threshold")
                ).count(),
                "open_tickets": EquipmentTicket.objects.filter(
                    clinic_tin=tin, branch_name__iexact=branch, status="Open"
                ).count(),
                "today_revenue": PaymentTransaction.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    created_at__date=today,
                ).aggregate(total=Sum("amount"))["total"]
                or 0,
                "today_refunds": RefundTransaction.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    created_at__date=today,
                ).aggregate(total=Sum("amount"))["total"]
                or 0,
                "pending_forgiven": ForgivenAmount.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    status="pending",
                ).count(),
            }
        )


def _billing_report(payments, refunds, start, end, tin, branch):
    """Money that did not arrive as cash, and why: discounts, forgiven amounts, VAT, refunds, merged and proxy payments."""
    when = lambda dt: timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else ""
    gross = payments.aggregate(t=Sum("amount"))["t"] or 0
    refunded = refunds.aggregate(t=Sum("amount"))["t"] or 0
    vat = payments.aggregate(t=Sum("vat_amount"))["t"] or 0
    discounts = payments.filter(discount_amount__gt=0).select_related("encounter__patient").order_by("-created_at")
    forgiven = (
        ForgivenAmount.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            status="applied",
            updated_at__date__gte=start,
            updated_at__date__lte=end,
        )
        .select_related("encounter__patient")
        .order_by("-updated_at")
    )
    by_invoice = {}
    for pay in payments.exclude(invoice_number="").select_related("encounter__patient").order_by("-created_at"):
        by_invoice.setdefault(pay.invoice_number, []).append(pay)
    merged = [
        {
            "invoice": inv,
            "when": when(rows[0].created_at),
            "patient": rows[0].encounter.patient.full_name,
            "by": rows[0].processed_by_name,
            "parts": [{"method": r.tender_method, "amount": r.amount} for r in rows],
            "total": sum((r.amount for r in rows), Decimal("0")),
        }
        for inv, rows in by_invoice.items()
        if len(rows) > 1 and len({r.encounter_id for r in rows}) == 1
    ]
    proxy = payments.filter(paid_by_patient__isnull=False).select_related("encounter__patient", "paid_by_patient").order_by("-created_at")
    return {
        "gross_revenue": gross,
        "refunds": refunded,
        "net_revenue": gross - refunded,
        "vat_collected": vat,
        "vat_percent": billing.VAT_PERCENT,
        "discount_total": sum((p.discount_amount for p in discounts), Decimal("0")),
        "forgiven_total": sum((f.amount for f in forgiven), Decimal("0")),
        "discounts": [
            {"when": when(p.created_at), "patient": p.encounter.patient.full_name, "amount": p.discount_amount,
             "rule": p.discount_rule_name, "reason": p.discount_reason, "by": p.processed_by_name}
            for p in discounts[:200]
        ],
        "forgiven": [
            {"when": when(f.updated_at), "patient": f.encounter.patient.full_name, "amount": f.amount,
             "reason": f.reason, "by": f.requested_by_name, "approved_by": f.decided_by_name}
            for f in forgiven[:200]
        ],
        "refund_rows": [
            {"when": when(r.created_at), "patient": r.encounter.patient.full_name, "amount": r.amount,
             "reason": r.reason, "rule": r.rule_name, "by": r.processed_by_name}
            for r in refunds.select_related("encounter__patient").order_by("-created_at")[:200]
        ],
        "merged_payments": merged[:200],
        "paid_for_others": [
            {"when": when(p.created_at), "payer": p.paid_by_patient.full_name, "patient": p.encounter.patient.full_name,
             "amount": p.amount, "by": p.processed_by_name}
            for p in proxy[:200]
        ],
    }


def _month_end(year: int, month: int):
    import calendar
    from datetime import date

    return date(year, month, calendar.monthrange(year, month)[1])


def _report_period_bounds(period: str, anchor=None):
    """Window of `period` that contains `anchor` (default: today), capped at today."""
    today = timezone.localdate()
    anchor = min(anchor or today, today)
    key = (period or "day").strip().lower().replace("-", "_")
    if key in {"day", "daily"}:
        start, end, label = anchor, anchor, "Daily"
    elif key in {"month", "monthly"}:
        start = anchor.replace(day=1)
        end = _month_end(anchor.year, anchor.month)
        label = "Monthly"
    elif key in {"quarter", "quarterly"}:
        q = (anchor.month - 1) // 3
        start = anchor.replace(month=q * 3 + 1, day=1)
        end = _month_end(anchor.year, q * 3 + 3)
        label = "Quarterly"
    elif key in {"half", "half_year", "halfyear"}:
        first = anchor.month <= 6
        start = anchor.replace(month=1 if first else 7, day=1)
        end = _month_end(anchor.year, 6 if first else 12)
        label = "Half-year"
    elif key in {"year", "yearly", "annual"}:
        start = anchor.replace(month=1, day=1)
        end = _month_end(anchor.year, 12)
        label = "Yearly"
    else:
        start, end, label = anchor, anchor, "Daily"
        key = "day"
    return key, label, start, min(end, today)


def _parse_anchor_date(params):
    """?date=YYYY-MM-DD -> date, None when absent. ValueError when malformed."""
    raw = (params.get("date") or "").strip()
    if not raw:
        return None
    from datetime import date

    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("Date must be YYYY-MM-DD.")


MAX_CUSTOM_RANGE_DAYS = 731


def _parse_custom_range(params):
    """Return (start, end) for ?from=YYYY-MM-DD&to=YYYY-MM-DD, or None if absent.

    Raises ValueError with a user-facing message when present but invalid.
    """
    raw_from, raw_to = params.get("from"), params.get("to")
    if not raw_from and not raw_to:
        return None
    from datetime import date

    try:
        start = date.fromisoformat((raw_from or "").strip())
        end = date.fromisoformat((raw_to or raw_from or "").strip())
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.")
    if end < start:
        raise ValueError("The end date is before the start date.")
    if (end - start).days > MAX_CUSTOM_RANGE_DAYS:
        raise ValueError("Choose a range of two years or less.")
    return start, end


class ReportsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can open clinic reports."}, status=403)
        tin = tin_of(request.user)
        branch = branch_of(request.user)
        try:
            anchor = _parse_anchor_date(request.query_params)
            custom = _parse_custom_range(request.query_params)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        period_key, period_label, start, end = _report_period_bounds(
            request.query_params.get("period") or "day", anchor
        )
        if custom:
            start, end = custom
            period_key, period_label = "custom", "Custom range"
        encounters = Encounter.objects.filter(
            clinic_tin=tin,
            branch_name__iexact=branch,
            opened_at__date__gte=start,
            opened_at__date__lte=end,
        )
        payments = PaymentTransaction.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        orders = ClinicalOrder.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        revenue = payments.aggregate(total=Sum("amount"))["total"] or 0
        refunds = RefundTransaction.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
        refund_total = refunds.aggregate(total=Sum("amount"))["total"] or 0
        billing_report = _billing_report(payments, refunds, start, end, tin, branch)
        referral_patients = [
            {
                "patient": e.patient.full_name,
                "when": timezone.localtime(e.opened_at).strftime("%Y-%m-%d %H:%M"),
                "referred_from": e.referral_from.encounter.branch_name,
                "referred_to": branch,
                "department": e.referral_from.to_department,
                "diagnosis": e.referral_from.diagnosis,
                "priority": e.priority,
                "approved_by": getattr(e.referral_from.approved_by, "username", ""),
                "visit": e.number,
            }
            for e in encounters.filter(referral_from__isnull=False).select_related(
                "patient", "referral_from", "referral_from__encounter", "referral_from__approved_by"
            )[:200]
        ]
        priority_counts = dict(encounters.values_list("priority").annotate(n=Count("id")))
        priority_report = [
            {"priority": key, "label": label, "count": priority_counts.get(key, 0)} for key, label in PRIORITIES
        ]
        assignments = TreatmentAssignment.objects.filter(
            encounter__clinic_tin=tin, encounter__branch_name__iexact=branch,
            created_at__date__gte=start, created_at__date__lte=end,
        )
        treatment_report = {
            "assigned": assignments.count(),
            "with_results": sum(1 for a in assignments.prefetch_related("fields") if any(f.value for f in a.fields.all())),
            "expired_unfilled": assignments.filter(status="expired").count(),
        }
        expiry_warnings = orders.exclude(expiry_warning="").count()
        referred_out = Referral.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).exclude(approval_status="disqualified").count()
        appt_counts = dict(
            Appointment.objects.filter(
                clinic_tin=tin, branch_name__iexact=branch,
                scheduled_at__date__gte=start, scheduled_at__date__lte=end,
            )
            .values_list("status")
            .annotate(n=Count("id"))
        )
        appointment_report = {
            "total": sum(appt_counts.values()),
            "by_status": [
                {"status": key, "label": label, "count": appt_counts.get(key, 0)}
                for key, label in Appointment.STATUS_CHOICES
            ],
        }
        billable_items = BillableItem.objects.filter(
            encounter__clinic_tin=tin,
            encounter__branch_name__iexact=branch,
            encounter__opened_at__date__gte=start,
            encounter__opened_at__date__lte=end,
        )
        visits = encounters.count()
        avg_revenue = float(revenue) / visits if visits else 0

        revenue_by_day = list(
            payments.annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("day")
        )
        visits_by_day = list(
            encounters.annotate(day=TruncDate("opened_at"))
            .values("day")
            .annotate(count=Count("id"))
            .order_by("day")
        )
        tender_breakdown = list(
            payments.values("tender_method")
            .annotate(total=Sum("amount"), count=Count("id"))
            .order_by("-total")
        )
        arrival_breakdown = list(
            encounters.values("arrival_type")
            .annotate(count=Count("id"))
            .order_by("-count")
        )
        referral_status = list(
            Referral.objects.filter(
                encounter__clinic_tin=tin,
                encounter__branch_name__iexact=branch,
                created_at__date__gte=start,
                created_at__date__lte=end,
            )
            .values("approval_status", "destination_kind")
            .annotate(count=Count("id"))
            .order_by("destination_kind", "approval_status")
        )
        top_services = list(
            billable_items.values("description", "department")
            .annotate(count=Count("id"), revenue=Sum("paid_amount"))
            .order_by("-count")[:8]
        )
        order_status = list(
            orders.values("order_type", "status")
            .annotate(count=Count("id"))
            .order_by("order_type", "status")
        )

        return Response(
            {
                "period": period_key,
                "billing": billing_report,
                "referral_patients": referral_patients,
                "priority_breakdown": priority_report,
                "treatment": treatment_report,
                "expiry_warnings": expiry_warnings,
                "referred_out": referred_out,
                "appointments": appointment_report,
                "period_label": period_label,
                "anchor_date": min(anchor or timezone.localdate(), timezone.localdate()).isoformat(),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "branch_name": branch,
                "summary": {
                    "visits": visits,
                    "new_patients": Patient.objects.filter(
                        clinic_tin=tin,
                        branch_name__iexact=branch,
                        created_at__date__gte=start,
                        created_at__date__lte=end,
                    ).count(),
                    "open_encounters": encounters.exclude(
                        status=Encounter.STATUS_CLOSED
                    ).count(),
                    "closed_encounters": encounters.filter(
                        status=Encounter.STATUS_CLOSED
                    ).count(),
                    "revenue": revenue,
                    "refunds": refund_total,
                    "net_revenue": (revenue or 0) - (refund_total or 0),
                    "payment_count": payments.count(),
                    "avg_revenue_per_visit": round(avg_revenue, 2),
                    "billable_items": billable_items.count(),
                    "lab_orders": orders.filter(order_type="lab").count(),
                    "radiology_orders": orders.filter(order_type="radiology").count(),
                    "prescriptions": orders.filter(order_type="prescription").count(),
                    "referrals": Referral.objects.filter(
                        encounter__clinic_tin=tin,
                        encounter__branch_name__iexact=branch,
                        created_at__date__gte=start,
                        created_at__date__lte=end,
                    ).count(),
                    "low_stock": Medicine.objects.filter(
                        clinic_tin=tin,
                        branch_name__iexact=branch,
                        on_hand__lte=F("min_threshold"),
                    ).count(),
                    "open_tickets": EquipmentTicket.objects.filter(
                        clinic_tin=tin, branch_name__iexact=branch, status="Open"
                    ).count(),
                },
                # Flat keys kept for older clients
                "visits": visits,
                "new_patients": Patient.objects.filter(
                    clinic_tin=tin,
                    branch_name__iexact=branch,
                    created_at__date__gte=start,
                    created_at__date__lte=end,
                ).count(),
                "closed_encounters": encounters.filter(status=Encounter.STATUS_CLOSED).count(),
                "revenue": revenue,
                "payment_count": payments.count(),
                "lab_orders": orders.filter(order_type="lab").count(),
                "radiology_orders": orders.filter(order_type="radiology").count(),
                "prescriptions": orders.filter(order_type="prescription").count(),
                "referrals": Referral.objects.filter(
                    encounter__clinic_tin=tin,
                    encounter__branch_name__iexact=branch,
                    created_at__date__gte=start,
                    created_at__date__lte=end,
                ).count(),
                "low_stock": Medicine.objects.filter(
                    clinic_tin=tin, branch_name__iexact=branch, on_hand__lte=F("min_threshold")
                ).count(),
                "open_tickets": EquipmentTicket.objects.filter(
                    clinic_tin=tin, branch_name__iexact=branch, status="Open"
                ).count(),
                "revenue_by_day": [
                    {
                        "date": row["day"].isoformat() if row["day"] else "",
                        "total": row["total"] or 0,
                        "count": row["count"],
                    }
                    for row in revenue_by_day
                ],
                "visits_by_day": [
                    {"date": row["day"].isoformat() if row["day"] else "", "count": row["count"]}
                    for row in visits_by_day
                ],
                "tender_breakdown": [
                    {
                        "method": row["tender_method"] or "unspecified",
                        "total": row["total"] or 0,
                        "count": row["count"],
                    }
                    for row in tender_breakdown
                ],
                "arrival_breakdown": [
                    {"arrival_type": row["arrival_type"] or "unknown", "count": row["count"]}
                    for row in arrival_breakdown
                ],
                "referral_breakdown": [
                    {
                        "kind": row["destination_kind"] or "internal",
                        "status": row["approval_status"] or "pending",
                        "count": row["count"],
                    }
                    for row in referral_status
                ],
                "top_services": [
                    {
                        "description": row["description"] or "Service",
                        "department": row["department"] or "",
                        "count": row["count"],
                        "revenue": row["revenue"] or 0,
                    }
                    for row in top_services
                ],
                "order_status_breakdown": [
                    {
                        "order_type": row["order_type"],
                        "status": row["status"],
                        "count": row["count"],
                    }
                    for row in order_status
                ],
            }
        )


def _pair_tins(a: str, b: str):
    left, right = sorted([(a or "").strip(), (b or "").strip()], key=str.lower)
    return left, right


def _serialize_org_message(message: OrgManagerMessage) -> dict:
    return {
        "id": message.id,
        "sender_tin": message.sender_tin,
        "sender_username": getattr(message.sender, "username", "") if message.sender else "",
        "body": message.body,
        "image_url": message.image_url,
        "read_by_recipient": message.read_by_recipient,
        "created_at": message.created_at,
        "mine": False,
    }


class OrgManagerPeersView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can open org chat."}, status=403)
        my_tin = tin_of(request.user)
        org = org_tin_of(request.user)
        if not org:
            return Response({"peers": []})
        from api.models import UserProfile

        branch_rows = list(ClinicBranch.objects.filter(clinic_tin__iexact=org, is_active=True))
        op_tins = {(b.operational_tin() or "").strip() for b in branch_rows}
        op_tins.discard("")
        op_tins.discard(my_tin)
        profiles = (
            UserProfile.objects.filter(role__iexact="manager", clinic_tin__in=op_tins, is_active=True)
            .select_related("user")
            .order_by("branch_name", "clinic_tin")
        )
        peers = []
        for profile in profiles:
            peer_tin = (profile.clinic_tin or "").strip()
            if not peer_tin or peer_tin.lower() == my_tin.lower():
                continue
            tin_a, tin_b = _pair_tins(my_tin, peer_tin)
            thread = OrgManagerThread.objects.filter(
                org_tin__iexact=org, tin_a=tin_a, tin_b=tin_b
            ).first()
            unread = 0
            if thread:
                unread = thread.messages.filter(
                    read_by_recipient=False
                ).exclude(sender_tin__iexact=my_tin).count()
            peers.append(
                {
                    "clinic_tin": peer_tin,
                    "branch_name": profile.branch_name or peer_tin,
                    "clinic_name": profile.clinic_name or "",
                    "username": getattr(profile.user, "username", ""),
                    "unread_count": unread,
                }
            )
        return Response({"org_tin": org, "peers": peers})


class OrgManagerThreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can open org chat."}, status=403)
        my_tin = tin_of(request.user)
        peer_tin = (request.query_params.get("peer_tin") or "").strip()
        if not peer_tin:
            return Response({"detail": "peer_tin is required."}, status=400)
        org = org_tin_of(request.user)
        peer_org = org_tin_of_tin(peer_tin)
        if not org or peer_org.lower() != org.lower():
            return Response({"detail": "Peer is not in the same organization."}, status=403)
        tin_a, tin_b = _pair_tins(my_tin, peer_tin)
        thread, _ = OrgManagerThread.objects.get_or_create(
            org_tin=org, tin_a=tin_a, tin_b=tin_b
        )
        thread.messages.filter(read_by_recipient=False).exclude(
            sender_tin__iexact=my_tin
        ).update(read_by_recipient=True)
        messages = []
        for message in thread.messages.select_related("sender").all():
            payload = _serialize_org_message(message)
            payload["mine"] = (message.sender_tin or "").lower() == my_tin.lower()
            messages.append(payload)
        return Response(
            {
                "thread": {
                    "id": thread.id,
                    "org_tin": thread.org_tin,
                    "peer_tin": peer_tin,
                },
                "messages": messages,
                "unread_count": 0,
            }
        )


class OrgManagerSendView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can open org chat."}, status=403)
        my_tin = tin_of(request.user)
        peer_tin = (request.data.get("peer_tin") or "").strip()
        body = (request.data.get("body") or "").strip()
        image_url = (request.data.get("image_url") or "").strip()
        if not peer_tin:
            return Response({"detail": "peer_tin is required."}, status=400)
        if not body and not image_url:
            return Response({"detail": "Message body or image is required."}, status=400)
        org = org_tin_of(request.user)
        if not org or org_tin_of_tin(peer_tin).lower() != org.lower():
            return Response({"detail": "Peer is not in the same organization."}, status=403)
        tin_a, tin_b = _pair_tins(my_tin, peer_tin)
        thread, _ = OrgManagerThread.objects.get_or_create(
            org_tin=org, tin_a=tin_a, tin_b=tin_b
        )
        message = OrgManagerMessage.objects.create(
            thread=thread,
            sender=request.user,
            sender_tin=my_tin,
            body=body,
            image_url=image_url,
        )
        thread.save(update_fields=["updated_at"])
        payload = _serialize_org_message(message)
        payload["mine"] = True
        return Response(payload, status=201)


class OrgManagerUnreadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if role_of(request.user) != "manager":
            return Response({"unread_count": 0})
        my_tin = tin_of(request.user)
        org = org_tin_of(request.user)
        if not my_tin or not org:
            return Response({"unread_count": 0})
        count = OrgManagerMessage.objects.filter(
            thread__org_tin__iexact=org,
            read_by_recipient=False,
        ).exclude(sender_tin__iexact=my_tin).filter(
            Q(thread__tin_a__iexact=my_tin) | Q(thread__tin_b__iexact=my_tin)
        ).count()
        return Response({"unread_count": count})


def org_tin_of_tin(clinic_tin: str) -> str:
    from tenants.services import org_tin_for

    return org_tin_for(clinic_tin)

