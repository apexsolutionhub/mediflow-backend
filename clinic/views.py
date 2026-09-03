from decimal import Decimal

from django.db import transaction
from django.db.models import F, Q, Sum
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


class TenantScopedMixin:
    pagination_class = FlexiblePagination

    def get_permissions(self):
        return [IsAuthenticated(), TenantBillingAccessPermission()]

    def get_clinic_tin(self):
        return tin_of(self.request.user)

    def require_roles(self, *roles):
        if role_of(self.request.user) not in {r.lower() for r in roles}:
            return False
        return True


class PatientViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = PatientSerializer

    def get_queryset(self):
        qs = Patient.objects.filter(clinic_tin=self.get_clinic_tin())
        q = self.request.query_params.get("q")
        if q:
            qs = qs.filter(Q(full_name__icontains=q) | Q(mrn__icontains=q) | Q(phone__icontains=q))
        return qs

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        count = Patient.objects.filter(clinic_tin=tin).count() + 1
        serializer.save(clinic_tin=tin, mrn=f"MRN-{count:05d}")


class EncounterViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EncounterSerializer

    def get_queryset(self):
        qs = (
            Encounter.objects.filter(clinic_tin=self.get_clinic_tin())
            .select_related("patient", "chart")
            .prefetch_related("billables", "orders", "payments", "refunds", "nurse_notes")
        )
        today = self.request.query_params.get("today")
        if today == "1":
            start = timezone.localdate()
            qs = qs.filter(opened_at__date=start).exclude(status=Encounter.STATUS_CLOSED)
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
        target = getattr(serializer, "child", serializer)
        if "patient_id" in getattr(target, "fields", {}):
            target.fields["patient_id"].queryset = Patient.objects.filter(clinic_tin=tin)
        return serializer

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        day = timezone.now().strftime("%Y%m%d")
        seq = Encounter.objects.filter(clinic_tin=tin, number__startswith=f"ENC-{day}").count() + 1
        patient = serializer.validated_data.get("patient")
        arrival = serializer.validated_data.get("arrival_type") or "new"
        if arrival == "returning" and not patient:
            raise ValueError("Returning patients require an existing record.")
        encounter = serializer.save(
            clinic_tin=tin,
            number=f"ENC-{day}-{seq:04d}",
            opened_by=self.request.user,
            status=Encounter.STATUS_OPEN,
        )
        # PRD: consultation is billable on arrival — payment unlocks clinical units.
        consult = (
            BillableService.objects.filter(
                clinic_tin=tin, auto_add_on_registration=True, is_active=True
            ).first()
            or BillableService.objects.filter(clinic_tin=tin, code="CONSULT", is_active=True).first()
            or BillableService.objects.filter(
                clinic_tin=tin, department__iexact="Consultation", is_active=True
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
        payload["external_prescriptions"] = [
            {
                "id": order.id,
                "details": order.details,
                "medicine_name": order.medicine.name if order.medicine_id else "",
            }
            for order in external_rx
        ]
        return Response(payload)


class BillableServiceViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = BillableServiceSerializer

    def get_queryset(self):
        return BillableService.objects.filter(clinic_tin=self.get_clinic_tin())

    def _clear_other_auto_add(self, tin, keep_pk=None):
        qs = BillableService.objects.filter(clinic_tin=tin, auto_add_on_registration=True)
        if keep_pk:
            qs = qs.exclude(pk=keep_pk)
        qs.update(auto_add_on_registration=False)

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        if serializer.validated_data.get("auto_add_on_registration"):
            self._clear_other_auto_add(tin)
        serializer.save(clinic_tin=tin)

    def perform_update(self, serializer):
        tin = self.get_clinic_tin()
        if serializer.validated_data.get("auto_add_on_registration"):
            self._clear_other_auto_add(tin, keep_pk=serializer.instance.pk)
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
            pk=request.data.get("encounter"), clinic_tin=self.get_clinic_tin()
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
            pk=request.data.get("encounter"), clinic_tin=self.get_clinic_tin()
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
        qs = ClinicalOrder.objects.filter(encounter__clinic_tin=self.get_clinic_tin())
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
        if encounter.clinic_tin != self.get_clinic_tin():
            raise PermissionError("Cross-tenant order blocked.")
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
                    pk=int(medicine_id), clinic_tin=self.get_clinic_tin()
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
                    pk=int(service_id), clinic_tin=self.get_clinic_tin()
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
        medicine = Medicine.objects.filter(pk=medicine_id, clinic_tin=self.get_clinic_tin()).first()
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
        return DoctorChart.objects.filter(encounter__clinic_tin=self.get_clinic_tin())

    def create(self, request, *args, **kwargs):
        encounter = Encounter.objects.filter(
            pk=request.data.get("encounter"), clinic_tin=self.get_clinic_tin()
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
        return NurseNote.objects.filter(encounter__clinic_tin=self.get_clinic_tin())

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class AppointmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer

    def get_queryset(self):
        qs = Appointment.objects.filter(clinic_tin=self.get_clinic_tin()).select_related("patient")
        if self.request.query_params.get("today") == "1":
            qs = qs.filter(scheduled_at__date=timezone.localdate())
        return qs

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin(), created_by=self.request.user)


class MedicineViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = MedicineSerializer

    def get_queryset(self):
        return Medicine.objects.filter(clinic_tin=self.get_clinic_tin())

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin())


class DepartmentViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = DepartmentSerializer

    def get_queryset(self):
        qs = Department.objects.filter(clinic_tin=self.get_clinic_tin())
        branch = (self.request.query_params.get("branch") or "").strip()
        if branch:
            qs = qs.filter(Q(branch_name__iexact=branch) | Q(branch_name=""))
        return qs

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin())

    def perform_update(self, serializer):
        instance = serializer.instance
        old_name = instance.name
        updated = serializer.save()
        if old_name != updated.name:
            BillableService.objects.filter(
                clinic_tin=updated.clinic_tin, department=old_name
            ).update(department=updated.name)

    def perform_destroy(self, instance):
        in_use = BillableService.objects.filter(
            clinic_tin=instance.clinic_tin, department=instance.name
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
        return ClinicBranch.objects.filter(clinic_tin=self.get_clinic_tin())

    def perform_create(self, serializer):
        tin = self.get_clinic_tin()
        is_main = bool(serializer.validated_data.get("is_main"))
        if is_main:
            ClinicBranch.objects.filter(clinic_tin=tin, is_main=True).update(is_main=False)
        branch = serializer.save(clinic_tin=tin)
        if not ClinicBranch.objects.filter(clinic_tin=tin).exclude(pk=branch.pk).exists():
            if not branch.is_main:
                branch.is_main = True
                branch.save(update_fields=["is_main"])
        return branch

    def perform_update(self, serializer):
        tin = self.get_clinic_tin()
        if serializer.validated_data.get("is_main"):
            ClinicBranch.objects.filter(clinic_tin=tin, is_main=True).exclude(
                pk=serializer.instance.pk
            ).update(is_main=False)
        serializer.save()


class TicketViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = EquipmentTicketSerializer

    def get_queryset(self):
        return EquipmentTicket.objects.filter(clinic_tin=self.get_clinic_tin())

    def perform_create(self, serializer):
        serializer.save(clinic_tin=self.get_clinic_tin(), created_by=self.request.user)


class ReferralViewSet(TenantScopedMixin, viewsets.ModelViewSet):
    serializer_class = ReferralSerializer

    def get_queryset(self):
        return Referral.objects.filter(encounter__clinic_tin=self.get_clinic_tin())

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)


class DashboardView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        tin = tin_of(request.user)
        today = timezone.localdate()
        encounters = Encounter.objects.filter(clinic_tin=tin)
        return Response(
            {
                "today_encounters": encounters.filter(opened_at__date=today).count(),
                "open_encounters": encounters.exclude(status=Encounter.STATUS_CLOSED).count(),
                "pending_payments": BillableItem.objects.filter(
                    encounter__clinic_tin=tin
                ).exclude(payment_status="PaymentApproved").count(),
                "lab_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    order_type="lab",
                    status="PaymentApproved",
                ).count(),
                "radiology_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    order_type="radiology",
                    status="PaymentApproved",
                ).count(),
                "results_ready": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    order_type__in=["lab", "radiology"],
                    status="Completed",
                ).count(),
                "rx_queue": ClinicalOrder.objects.filter(
                    encounter__clinic_tin=tin,
                    order_type="prescription",
                    status="PaymentApproved",
                ).count(),
                "low_stock": Medicine.objects.filter(
                    clinic_tin=tin, on_hand__lte=F("min_threshold")
                ).count(),
                "open_tickets": EquipmentTicket.objects.filter(clinic_tin=tin, status="Open").count(),
                "today_revenue": PaymentTransaction.objects.filter(
                    encounter__clinic_tin=tin, created_at__date=today
                ).aggregate(total=Sum("amount"))["total"]
                or 0,
            }
        )

