from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from tenants.permissions import TenantBillingAccessPermission

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
    OrgManagerMessage,
    OrgManagerThread,
    Patient,
    PaymentTransaction,
    Referral,
    RefundTransaction,
)
from .serializers import (
    AppointmentSerializer,
    BillableItemSerializer,
    BillableServiceSerializer,
    ClinicBranchSerializer,
    ClinicalOrderSerializer,
    DepartmentSerializer,
    DoctorChartSerializer,
    EncounterSerializer,
    EquipmentTicketSerializer,
    MedicineSerializer,
    NurseNoteSerializer,
    PatientSerializer,
    PaymentTransactionSerializer,
    ReferralSerializer,
    RefundTransactionSerializer,
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


class PatientViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = PatientSerializer

    def get_queryset(self):
        qs = Patient.objects.filter(clinic_tin=self.get_clinic_tin())
        qs = scope_branch(qs, self.request.user)
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(mrn__icontains=q) | Q(phone__icontains=q))
        return qs

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        count = Patient.objects.filter(clinic_tin=tin, branch_name__iexact=branch).count() + 1
        serializer.save(clinic_tin=tin, branch_name=branch, mrn=f"MRN-{count:05d}")


class EncounterViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EncounterSerializer

    def get_queryset(self):
        qs = (
            Encounter.objects.filter(clinic_tin=self.get_clinic_tin())
            .select_related("patient", "chart")
            .prefetch_related("billables", "orders", "payments", "refunds", "nurse_notes")
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
        patient = serializer.validated_data.get("patient")
        arrival = serializer.validated_data.get("arrival_type") or "new"
        if arrival == "returning" and not patient:
            raise ValueError("Returning patients require an existing record.")
        encounter = serializer.save(
            clinic_tin=tin,
            branch_name=branch,
            number=f"ENC-{day}-{seq:04d}",
            opened_by=self.request.user,
            status=Encounter.STATUS_OPEN,
        )
        # PRD: consultation is billable on arrival — payment unlocks clinical units.
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

    @action(detail=True, methods=["post"])
    def checkout(self, request, pk=None):
        encounter = self.get_object()
        unpaid = encounter.billables.exclude(payment_status="PaymentApproved")
        if unpaid.exists():
            return Response(
                {"detail": "Checkout blocked until required billable items are approved."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        external_rx = list(
            encounter.orders.filter(
                order_type="prescription",
                fulfillment=ClinicalOrder.FULFILLMENT_EXTERNAL,
            ).select_related("medicine")
        )
        encounter.status = Encounter.STATUS_CLOSED
        encounter.closed_at = timezone.now()
        encounter.save(update_fields=["status", "closed_at"])
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
            {
                "id": order.id,
                "details": order.details,
                "medicine_name": medicine_label(order),
            }
            for order in external_rx
        ]
        upcoming = (
            Appointment.objects.filter(
                clinic_tin=encounter.clinic_tin,
                branch_name__iexact=encounter.branch_name or self.get_branch_name(),
                patient_id=encounter.patient_id,
                scheduled_at__gte=timezone.now(),
            )
            .order_by("scheduled_at")[:20]
        )
        payload["follow_up_appointments"] = AppointmentSerializer(upcoming, many=True).data
        payload["referrals"] = ReferralSerializer(
            encounter.referrals.all().order_by("created_at"), many=True
        ).data
        return Response(payload)


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

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
        if serializer.validated_data.get("auto_add_on_registration"):
            self._clear_other_auto_add(tin, branch)
        serializer.save(clinic_tin=tin, branch_name=branch)

    def perform_update(self, serializer):
        tin = self.get_clinic_tin()
        branch = self.get_branch_name()
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
        instance.delete()


class PaymentViewSet(TenantScopedMixin, viewsets.GenericViewSet):
    serializer_class = PaymentTransactionSerializer

    @action(detail=False, methods=["post"])
    def approve(self, request):
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Only reception can approve payments."}, status=403)
        encounter = Encounter.objects.filter(
            pk=request.data.get("encounter"),
            clinic_tin=self.get_clinic_tin(),
            branch_name__iexact=self.get_branch_name(),
        ).first()
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        amount = Decimal(str(request.data.get("amount") or 0))
        method = request.data.get("tender_method") or "cash"
        if amount <= 0:
            return Response({"detail": "Amount must be positive."}, status=400)

        remaining_items = list(encounter.billables.exclude(payment_status="PaymentApproved"))
        leftover = amount
        with transaction.atomic():
            for item in remaining_items:
                due = item.total_amount - item.paid_amount
                if due <= 0:
                    item.payment_status = "PaymentApproved"
                    item.save()
                    continue
                apply = min(due, leftover)
                item.paid_amount += apply
                leftover -= apply
                if item.paid_amount >= item.total_amount:
                    item.payment_status = "PaymentApproved"
                else:
                    item.payment_status = "PartialPayment"
                item.save()
                if leftover <= 0:
                    break
            for item in encounter.billables.filter(payment_status="PaymentApproved"):
                ClinicalOrder.objects.filter(billable=item, status="AwaitingPayment").update(
                    status="PaymentApproved"
                )
            receipt = f"RCPT-{timezone.now().strftime('%Y%m%d%H%M%S')}"
            pay = PaymentTransaction.objects.create(
                encounter=encounter,
                receipt_number=receipt,
                amount=amount,
                tender_method=method,
                processed_by=request.user,
                notes=request.data.get("notes", ""),
            )
            if encounter.status == Encounter.STATUS_OPEN:
                encounter.status = Encounter.STATUS_ACTIVE
                encounter.save(update_fields=["status"])
        return Response(PaymentTransactionSerializer(pay).data, status=201)

    @action(detail=False, methods=["post"])
    def refund(self, request):
        if role_of(request.user) not in {"reception", "manager"}:
            return Response({"detail": "Forbidden."}, status=403)
        encounter = Encounter.objects.filter(
            pk=request.data.get("encounter"),
            clinic_tin=self.get_clinic_tin(),
            branch_name__iexact=self.get_branch_name(),
        ).first()
        if not encounter:
            return Response({"detail": "Encounter not found."}, status=404)
        refund = RefundTransaction.objects.create(
            encounter=encounter,
            amount=Decimal(str(request.data.get("amount") or 0)),
            reason=request.data.get("reason") or "Refund",
            processed_by=request.user,
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
        return qs.select_related("encounter", "encounter__patient").order_by("-updated_at")

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
        description = serializer.validated_data.get("details") or "Clinical order"
        price = Decimal("0")
        dept = "Laboratory"
        service = None
        medicine = None
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
        # In-stock medicine → clinic pharmacy; free-text name → outside pharmacy print.
        if serializer.validated_data.get("order_type") == "prescription":
            if medicine:
                fulfillment = ClinicalOrder.FULFILLMENT_CLINIC
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
            price = medicine.unit_price or Decimal("0")
            dept = "Pharmacy"
        elif serializer.validated_data.get("order_type") == "radiology":
            dept = "Radiology"
        quantity = max(1, int(getattr(service, "default_quantity", None) or 1)) if service else 1
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
        serializer.save(
            created_by=self.request.user,
            status=order_status,
            billable=billable,
            medicine=medicine,
            fulfillment=fulfillment
            if serializer.validated_data.get("order_type") == "prescription"
            else ClinicalOrder.FULFILLMENT_CLINIC,
        )

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
        order.result_text = request.data.get("result_text", order.result_text)
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
        if role_of(request.user) not in {"pharmacist", "manager"}:
            return Response({"detail": "Forbidden."}, status=403)
        order = self.get_object()
        if order.status != "PaymentApproved" and order.status != "InProgress":
            return Response({"detail": "Rx is not payment-approved."}, status=400)
        if order.order_type != "prescription":
            return Response({"detail": "Not a prescription."}, status=400)
        medicine_id = request.data.get("medicine")
        qty = int(request.data.get("quantity") or 1)
        medicine = Medicine.objects.filter(
            pk=medicine_id,
            clinic_tin=self.get_clinic_tin(),
            branch_name__iexact=self.get_branch_name(),
        ).first()
        if not medicine:
            return Response({"detail": "Medicine not found."}, status=404)
        if medicine.on_hand < qty:
            return Response({"detail": "Insufficient stock."}, status=400)
        medicine.on_hand -= qty
        medicine.save(update_fields=["on_hand"])
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
        return Response(DoctorChartSerializer(chart).data, status=200)


class NurseNoteViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = NurseNoteSerializer

    def get_queryset(self):
        return NurseNote.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        )

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AppointmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        qs = scope_branch(
            Appointment.objects.filter(clinic_tin=self.get_clinic_tin()).select_related("patient"),
            self.request.user,
        )
        if self.request.query_params.get("today") == "1":
            qs = qs.filter(scheduled_at__date=timezone.localdate())
        return qs

    def perform_create(self, serializer):
        serializer.save(
            clinic_tin=self.get_clinic_tin(),
            branch_name=self.get_branch_name(),
            created_by=self.request.user,
        )


class MedicineViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = MedicineSerializer

    def get_queryset(self):
        return scope_branch(
            Medicine.objects.filter(clinic_tin=self.get_clinic_tin()),
            self.request.user,
        )

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name())


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

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin(), branch_name=self.get_branch_name())

    def perform_update(self, serializer):
        instance = serializer.instance
        old_name = instance.name
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
        instance.delete()


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
        branch_tin = (serializer.validated_data.get("branch_tin") or self.get_clinic_tin()).strip()
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
        serializer.save(
            clinic_tin=self.get_clinic_tin(),
            branch_name=self.get_branch_name(),
            created_by=self.request.user,
        )


class ReferralViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = ReferralSerializer

    def get_queryset(self):
        qs = Referral.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        ).select_related(
            "encounter",
            "encounter__patient",
            "created_by",
            "approved_by",
        )
        status_filter = (self.request.query_params.get("approval_status") or "").strip()
        if status_filter:
            qs = qs.filter(approval_status=status_filter)
        kind = (self.request.query_params.get("destination_kind") or "").strip()
        if kind:
            qs = qs.filter(destination_kind=kind)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        encounter = serializer.validated_data.get("encounter")
        if encounter is not None:
            if encounter.clinic_tin != self.get_clinic_tin():
                raise PermissionError("Cross-tenant referral blocked.")
            if (encounter.branch_name or "").strip().lower() != self.get_branch_name().lower():
                raise PermissionError("Cross-branch referral blocked.")
        serializer.save(created_by=self.request.user)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can approve referrals."}, status=403)
        referral = self.get_object()
        if referral.approval_status == Referral.APPROVAL_APPROVED:
            return Response(ReferralSerializer(referral).data)
        referral.approval_status = Referral.APPROVAL_APPROVED
        referral.approved_by = request.user
        referral.approved_at = timezone.now()
        referral.save(
            update_fields=["approval_status", "approved_by", "approved_at"]
        )
        return Response(ReferralSerializer(referral).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can reject referrals."}, status=403)
        referral = self.get_object()
        referral.approval_status = Referral.APPROVAL_REJECTED
        referral.approved_by = request.user
        referral.approved_at = timezone.now()
        referral.save(
            update_fields=["approval_status", "approved_by", "approved_at"]
        )
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
            }
        )


def _report_period_bounds(period: str):
    today = timezone.localdate()
    key = (period or "day").strip().lower().replace("-", "_")
    if key in {"day", "daily"}:
        start = today
        label = "Daily"
    elif key in {"month", "monthly"}:
        start = today.replace(day=1)
        label = "Monthly"
    elif key in {"quarter", "quarterly"}:
        q = (today.month - 1) // 3
        start = today.replace(month=q * 3 + 1, day=1)
        label = "Quarterly"
    elif key in {"half", "half_year", "halfyear"}:
        start = today.replace(month=1 if today.month <= 6 else 7, day=1)
        label = "Half-year"
    elif key in {"year", "yearly", "annual"}:
        start = today.replace(month=1, day=1)
        label = "Yearly"
    else:
        start = today
        label = "Daily"
        key = "day"
    return key, label, start, today


class ReportsView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        if role_of(request.user) != "manager":
            return Response({"detail": "Only managers can open clinic reports."}, status=403)
        tin = tin_of(request.user)
        branch = branch_of(request.user)
        period_key, period_label, start, end = _report_period_bounds(
            request.query_params.get("period") or "day"
        )
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
                "period_label": period_label,
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

