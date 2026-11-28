"""Status Treatment: the doctor asks the nurse for measurements at a stage of an open visit.

The measurement fields are built like a lab test's result fields (name, type, unit, reference and critical
range, choices), so the nurse's readings are checked and flagged the same way. An assignment stops being
fillable when the patient checks out.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from . import billing, vitals
from .duplicates import text_key
from .models import Encounter, TreatmentAssignment, TreatmentField, TreatmentTemplate
from .views import TenantScopedMixin, role_of

RESULT_TYPES = {"numeric", "choice", "text"}
MAX_FIELDS = 40


def _decimal(value, label):
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValidationError({"detail": f"{label}: enter a number."})


def field_data(f):
    param = f.as_parameter()
    return {
        "id": f.id,
        "name": f.name,
        "result_type": f.result_type,
        "unit": f.unit,
        "ref_low": f.ref_low,
        "ref_high": f.ref_high,
        "critical_low": f.critical_low,
        "critical_high": f.critical_high,
        "choices": f.choices,
        "choice_list": param.choice_list,
        "reference_text": param.reference_text,
        "value": f.value,
        "flag": f.flag,
        "sort_order": f.sort_order,
    }


def assignment_data(a):
    fields = list(a.fields.all())
    return {
        "id": a.id,
        "encounter": a.encounter_id,
        "encounter_number": a.encounter.number,
        "patient_name": a.encounter.patient.full_name,
        "patient_mrn": a.encounter.patient.mrn,
        "encounter_status": a.encounter.status,
        "stage": a.stage,
        "stage_label": vitals.STAGES.get(a.stage, a.stage),
        "status": a.status,
        "created_by_name": a.created_by_name,
        "created_at": a.created_at,
        "expired_at": a.expired_at,
        "results_by_name": a.results_by_name,
        "results_at": a.results_at,
        "reassigned_from": a.reassigned_from_id,
        "filled_count": sum(1 for f in fields if f.value),
        "fields": [field_data(f) for f in fields],
    }


class TreatmentAssignmentViewSet(TenantScopedMixin, viewsets.GenericViewSet):
    def get_queryset(self):
        qs = TreatmentAssignment.objects.filter(
            encounter__clinic_tin=self.get_clinic_tin(),
            encounter__branch_name__iexact=self.get_branch_name(),
        ).select_related("encounter", "encounter__patient").prefetch_related("fields")
        params = self.request.query_params
        if params.get("encounter"):
            qs = qs.filter(encounter_id=params["encounter"])
        if params.get("status") in ("active", "expired"):
            qs = qs.filter(status=params["status"])
        return qs

    def _open_visit(self, pk):
        encounter = Encounter.objects.filter(
            pk=pk, clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        ).select_related("patient").first()
        if encounter is None:
            raise ValidationError({"detail": "Choose a patient with an open visit."})
        if encounter.status == Encounter.STATUS_CLOSED:
            raise ValidationError({"detail": f"{encounter.patient.full_name} has already checked out."})
        return encounter

    @staticmethod
    def _clean_field(raw, index):
        name = str((raw or {}).get("name") or "").strip()
        label = name or f"Line {index}"
        if not name:
            raise ValidationError({"detail": f"Line {index}: give the measurement a name."})
        kind = str(raw.get("result_type") or "numeric")
        if kind not in RESULT_TYPES:
            raise ValidationError({"detail": f"{label}: choose numeric, choice or text."})
        out = {"name": name[:120], "result_type": kind, "unit": str(raw.get("unit") or "")[:32], "choices": ""}
        for key in ("ref_low", "ref_high", "critical_low", "critical_high"):
            out[key] = _decimal(raw.get(key), f"{label} {key.replace('_', ' ')}") if kind == "numeric" else None
        if kind == "numeric":
            low, high = out["ref_low"], out["ref_high"]
            if low is not None and high is not None and low > high:
                raise ValidationError({"detail": f"{label}: the low reference can't be above the high one."})
        if kind == "choice":
            options = [c.strip() for c in str(raw.get("choices") or "").split(",") if c.strip()]
            if len(options) < 2:
                raise ValidationError({"detail": f"{label}: a choice measurement needs at least two choices."})
            out["choices"] = ",".join(options)[:255]
        return out

    def _make(self, encounter, stage, fields, request, reassigned_from=None):
        assignment = TreatmentAssignment.objects.create(
            encounter=encounter,
            stage=stage,
            created_by=request.user,
            created_by_name=billing.acting_name(request),
            reassigned_from=reassigned_from,
        )
        TreatmentField.objects.bulk_create(
            [TreatmentField(assignment=assignment, sort_order=i, **spec) for i, spec in enumerate(fields)]
        )
        return assignment

    def list(self, request):
        rows = self.get_queryset()[:300]
        return Response([assignment_data(a) for a in rows])

    def create(self, request):
        if role_of(request.user) not in {"doctor", "manager"}:
            raise PermissionDenied("Only the doctor assigns treatment measurements.")
        encounter = self._open_visit(request.data.get("encounter"))
        stage = str(request.data.get("stage") or "")
        if stage not in vitals.STAGES:
            raise ValidationError({"stage": ["Choose the stage these measurements apply to."]})
        raw = request.data.get("fields")
        if not isinstance(raw, list) or not raw:
            raise ValidationError({"detail": "Add at least one measurement."})
        if len(raw) > MAX_FIELDS:
            raise ValidationError({"detail": f"At most {MAX_FIELDS} measurements at a time."})
        fields = [self._clean_field(r, i) for i, r in enumerate(raw, start=1)]
        names = [f["name"].casefold() for f in fields]
        if len(set(names)) != len(names):
            raise ValidationError({"detail": "Two measurements have the same name."})
        with transaction.atomic():
            assignment = self._make(encounter, stage, fields, request)
        return Response(assignment_data(assignment), status=201)

    @action(detail=True, methods=["post"])
    def results(self, request, pk=None):
        """The nurse enters readings. Anything left blank stays blank; filled readings are checked and flagged."""
        if role_of(request.user) not in {"nurse", "manager"}:
            raise PermissionDenied("Only the nurse fills the results.")
        assignment = self.get_object()
        if assignment.status != TreatmentAssignment.ACTIVE or assignment.encounter.status == Encounter.STATUS_CLOSED:
            raise ValidationError({"detail": "This assignment expired when the patient checked out."})
        values = request.data.get("values")
        if not isinstance(values, dict):
            raise ValidationError({"detail": "Send the readings."})
        by_id = {str(f.id): f for f in assignment.fields.all()}
        updates = []
        for key, raw in values.items():
            field = by_id.get(str(key))
            if field is None:
                raise ValidationError({"detail": "That measurement is not part of this assignment."})
            if str(raw if raw is not None else "").strip() == "":
                continue
            try:
                clean, flag = field.as_parameter().evaluate(raw)
            except ValueError as exc:
                raise ValidationError({"detail": str(exc)})
            field.value, field.flag = clean, flag
            updates.append(field)
        if not updates:
            raise ValidationError({"detail": "Enter at least one reading."})
        with transaction.atomic():
            TreatmentField.objects.bulk_update(updates, ["value", "flag", "updated_at"])
            assignment.results_by_name = billing.acting_name(request)
            assignment.results_at = timezone.now()
            assignment.save(update_fields=["results_by_name", "results_at", "updated_at"])
        assignment = self.get_queryset().get(pk=assignment.pk)
        return Response(assignment_data(assignment))

    @action(detail=True, methods=["post"])
    def reassign(self, request, pk=None):
        """Give the same measurements and stage to another patient with an open visit. Results start blank."""
        if role_of(request.user) not in {"doctor", "manager"}:
            raise PermissionDenied("Only the doctor reassigns treatment measurements.")
        source = self.get_object()
        target = self._open_visit(request.data.get("encounter"))
        if target.pk == source.encounter_id:
            raise ValidationError({"detail": "Choose a different patient."})
        specs = [
            {
                "name": f.name, "result_type": f.result_type, "unit": f.unit, "ref_low": f.ref_low,
                "ref_high": f.ref_high, "critical_low": f.critical_low, "critical_high": f.critical_high,
                "choices": f.choices,
            }
            for f in source.fields.all()
        ]
        with transaction.atomic():
            copy = self._make(target, source.stage, specs, request, reassigned_from=source)
        return Response(assignment_data(copy), status=201)


def _jsonable(spec):
    """A cleaned field spec as plain JSON (numbers as text), for saving in a template."""
    return {k: (str(v) if isinstance(v, Decimal) else ("" if v is None else v)) for k, v in spec.items()}


class TreatmentTemplateViewSet(TenantScopedMixin, viewsets.GenericViewSet):
    """Saved sets of measurements. The whole clinic branch can read them; the doctor creates and deletes."""

    def get_queryset(self):
        return TreatmentTemplate.objects.filter(
            clinic_tin=self.get_clinic_tin(), branch_name__iexact=self.get_branch_name()
        )

    @staticmethod
    def _row(t):
        return {
            "id": t.id,
            "name": t.name,
            "fields": t.fields,
            "field_count": len(t.fields or []),
            "created_by_name": t.created_by_name,
            "created_at": t.created_at,
        }

    def list(self, request):
        return Response([self._row(t) for t in self.get_queryset()])

    def create(self, request):
        if role_of(request.user) not in {"doctor", "manager"}:
            raise PermissionDenied("Only the doctor saves measurement sets.")
        name = " ".join(str(request.data.get("name") or "").split())
        if not name:
            raise ValidationError({"name": ["Give the set a name."]})
        raw = request.data.get("fields")
        if not isinstance(raw, list) or not raw:
            raise ValidationError({"detail": "Add at least one measurement."})
        if len(raw) > MAX_FIELDS:
            raise ValidationError({"detail": f"At most {MAX_FIELDS} measurements in a set."})
        fields = [TreatmentAssignmentViewSet._clean_field(r, i) for i, r in enumerate(raw, start=1)]
        names = [f["name"].casefold() for f in fields]
        if len(set(names)) != len(names):
            raise ValidationError({"detail": "Two measurements have the same name."})
        if any(text_key(t.name) == text_key(name) for t in self.get_queryset()):
            raise ValidationError({"name": [f"A set named {name} already exists."]})
        template = TreatmentTemplate.objects.create(
            clinic_tin=self.get_clinic_tin(),
            branch_name=self.get_branch_name(),
            name=name[:120],
            fields=[_jsonable(f) for f in fields],
            created_by_name=billing.acting_name(request),
        )
        return Response(self._row(template), status=201)

    def destroy(self, request, pk=None):
        if role_of(request.user) not in {"doctor", "manager"}:
            raise PermissionDenied("Only the doctor deletes measurement sets.")
        template = self.get_queryset().filter(pk=pk).first()
        if template is None:
            raise ValidationError({"detail": "That set no longer exists."})
        template.soft_delete()
        return Response(status=204)
