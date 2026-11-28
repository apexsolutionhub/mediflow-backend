"""Manager's patient history: every patient since the system started, with their visits and money.

The list is paged 10 at a time. On "All" each row is a patient (with a visit count to expand); with one patient
chosen the rows are that patient's visits. A separate detail call returns everything about one patient.
"""
from decimal import Decimal

from django.db.models import Count, Q
from rest_framework import viewsets
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.response import Response

from .models import Appointment, Encounter, Patient
from .serializers import AppointmentSerializer, EncounterSerializer, ReferralSerializer
from .views import TenantScopedMixin, role_of

PAGE = 10


def _money(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _visit_row(enc):
    payments = list(enc.payments.all())
    refunds = list(enc.refunds.all())
    methods = []
    for p in payments:
        label = p.tender_method.split(":")[0]
        if label not in methods:
            methods.append(label)
    paid = sum((_money(p.amount) for p in payments), Decimal("0"))
    refunded = sum((_money(r.amount) for r in refunds), Decimal("0"))
    return {
        "encounter": enc.id,
        "number": enc.number,
        "status": enc.status,
        "registered_at": enc.opened_at,
        "checked_out_at": enc.closed_at,
        "total_paid": paid,
        "refunded": refunded,
        "methods": methods,
        "priority": enc.priority,
        "referred_from": enc.referral_from.encounter.branch_name if enc.referral_from_id else "",
    }


class PatientHistoryViewSet(TenantScopedMixin, viewsets.ViewSet):
    def _check(self, request):
        if role_of(request.user) != "manager":
            raise PermissionDenied("Only the manager sees the patient history.")

    def _patients(self):
        return Patient.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        )

    def _visits(self, patient_ids):
        qs = (
            Encounter.objects.filter(patient_id__in=patient_ids)
            .select_related("referral_from__encounter")
            .prefetch_related("payments", "refunds")
            .order_by("-opened_at")
        )
        by_patient = {}
        for enc in qs:
            by_patient.setdefault(enc.patient_id, []).append(_visit_row(enc))
        return by_patient

    @staticmethod
    def _page(request):
        try:
            return max(1, int(request.query_params.get("page") or 1))
        except ValueError:
            return 1

    def list(self, request):
        self._check(request)
        page = self._page(request)
        q = (request.query_params.get("q") or "").strip()
        patients = self._patients()
        if q:
            patients = patients.filter(Q(full_name__icontains=q) | Q(mrn__icontains=q) | Q(phone__icontains=q))

        chosen = request.query_params.get("patient")
        if chosen:
            patient = patients.filter(pk=chosen).first()
            if patient is None:
                return Response({"mode": "visits", "count": 0, "page": 1, "page_size": PAGE, "results": []})
            visits = self._visits([patient.id]).get(patient.id, [])
            start = (page - 1) * PAGE
            rows = [
                {**v, "patient_id": patient.id, "mrn": patient.mrn, "patient_name": patient.full_name}
                for v in visits[start:start + PAGE]
            ]
            return Response({"mode": "visits", "count": len(visits), "page": page, "page_size": PAGE, "results": rows})

        total = patients.count()
        start = (page - 1) * PAGE
        chunk = list(patients.annotate(n=Count("encounters")).order_by("full_name", "id")[start:start + PAGE])
        visits = self._visits([p.id for p in chunk])
        rows = []
        for p in chunk:
            mine = visits.get(p.id, [])
            rows.append(
                {
                    "patient_id": p.id,
                    "mrn": p.mrn,
                    "patient_name": p.full_name,
                    "visit_count": len(mine),
                    "total_paid": sum((v["total_paid"] for v in mine), Decimal("0")),
                    "registered_at": p.created_at,
                    "visits": mine,
                }
            )
        return Response({"mode": "patients", "count": total, "page": page, "page_size": PAGE, "results": rows})

    def options(self, request):
        """The patient filter's choices: everyone, name first."""
        self._check(request)
        rows = self._patients().order_by("full_name", "id").values("id", "mrn", "full_name")[:1000]
        return Response([{"id": r["id"], "mrn": r["mrn"], "name": r["full_name"]} for r in rows])

    def record(self, request, pk=None):
        """The chart, orders with results, follow-ups, referrals, pharmacy orders and payments of one patient."""
        self._check(request)
        patient = self._patients().filter(pk=pk).first()
        if patient is None:
            raise NotFound("Patient not found.")
        encounters = (
            Encounter.objects.filter(patient=patient)
            .select_related("chart", "referral_from__encounter")
            .prefetch_related(
                "billables", "orders", "orders__radiology_report", "orders__result_values",
                "orders__billable__service", "orders__medicine", "payments", "refunds", "nurse_notes", "referrals",
            )
            .order_by("-opened_at")
        )
        visits = []
        for enc in encounters:
            data = EncounterSerializer(enc).data
            orders = data.get("orders") or []
            visits.append(
                {
                    "encounter": enc.id,
                    "number": enc.number,
                    "status": enc.status,
                    "registered_at": enc.opened_at,
                    "checked_out_at": enc.closed_at,
                    "priority": enc.priority,
                    "referred_from": enc.referral_from.encounter.branch_name if enc.referral_from_id else "",
                    "chart": data.get("chart"),
                    "lab_radiology": [o for o in orders if o["order_type"] in ("lab", "radiology")],
                    "pharmacy": [o for o in orders if o["order_type"] == "prescription"],
                    "referrals": ReferralSerializer(enc.referrals.all(), many=True).data,
                    "payments": data.get("payments") or [],
                    "refunds": data.get("refunds") or [],
                    "total_paid": sum((_money(p["amount"]) for p in data.get("payments") or []), Decimal("0")),
                }
            )
        appointments = Appointment.objects.filter(patient=patient).order_by("-scheduled_at")[:50]
        return Response(
            {
                "patient": {
                    "id": patient.id, "mrn": patient.mrn, "name": patient.full_name, "age": patient.age,
                    "gender": patient.gender, "phone": patient.phone, "address": patient.address,
                    "allergies": patient.allergies, "registered_at": patient.created_at,
                },
                "visits": visits,
                "follow_ups": AppointmentSerializer(appointments, many=True).data,
            }
        )
