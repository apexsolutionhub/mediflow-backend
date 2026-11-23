"""Attach standard structured result fields to lab services.

Usage:
  python manage.py seed_lab_catalog                 # every clinic/branch with services
  python manage.py seed_lab_catalog --tin 123 --branch Main

Existing lab services whose name matches a template get the template's
parameters (only if they have none yet). Templates with no matching service
are created INACTIVE with price 0 so the manager sets a price and enables them.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from clinic.models import BillableService, LabTestParameter

N, C, T = "numeric", "choice", "text"
NEG_POS = ("Negative,Positive", "Positive")
NR_R = ("Non-reactive,Reactive", "Reactive")
DIP = ("Negative,Trace,+,++,+++", "+,++,+++")


def num(name, unit, low=None, high=None, crit_low=None, crit_high=None):
    return dict(name=name, result_type=N, unit=unit, ref_low=low, ref_high=high,
                critical_low=crit_low, critical_high=crit_high)


def choice(name, pair):
    return dict(name=name, result_type=C, choices=pair[0], abnormal_choices=pair[1])


TEMPLATES = {
    "Complete Blood Count (CBC)": [
        num("Hemoglobin", "g/dL", 12, 17, 7, 20),
        num("Hematocrit", "%", 36, 50),
        num("WBC", "x10³/µL", 4, 11, 2, 30),
        num("Neutrophils", "%", 40, 70),
        num("Lymphocytes", "%", 20, 45),
        num("Platelets", "x10³/µL", 150, 450, 20, 1000),
        num("MCV", "fL", 80, 100),
    ],
    "Fasting Blood Sugar": [num("Glucose (fasting)", "mg/dL", 70, 100, 50, 400)],
    "Random Blood Sugar": [num("Glucose (random)", "mg/dL", 70, 140, 50, 400)],
    "HbA1c": [num("HbA1c", "%", 4, 5.6)],
    "Malaria RDT": [choice("Malaria RDT", NEG_POS)],
    "Malaria Blood Film": [
        choice("Malaria parasite", NEG_POS),
        dict(name="Species", result_type=C, choices="P. falciparum,P. vivax,Mixed,Not applicable"),
        dict(name="Density", result_type=C, choices="Not applicable,+,++,+++,++++", abnormal_choices="+,++,+++,++++"),
    ],
    "Pregnancy Test (HCG)": [dict(name="HCG", result_type=C, choices="Negative,Positive")],
    "HIV Test": [choice("HIV antibody", NR_R)],
    "HBsAg": [choice("HBsAg", NR_R)],
    "HCV Antibody": [choice("HCV antibody", NR_R)],
    "VDRL": [choice("VDRL", NR_R)],
    "H. pylori Antigen": [choice("H. pylori antigen", NEG_POS)],
    "Blood Group & Rh": [dict(name="Blood group", result_type=C, choices="A+,A-,B+,B-,AB+,AB-,O+,O-")],
    "Urinalysis": [
        choice("Protein", DIP),
        choice("Glucose", DIP),
        choice("Ketones", DIP),
        choice("Blood", DIP),
        choice("Leukocytes", DIP),
        choice("Nitrite", NEG_POS),
        num("pH", "", 4.5, 8),
        num("Specific gravity", "", 1.005, 1.030),
    ],
    "Stool Examination": [
        choice("Ova / parasites", NEG_POS),
        choice("Occult blood", NEG_POS),
    ],
    "Liver Function Test (LFT)": [
        num("ALT", "U/L", 7, 56),
        num("AST", "U/L", 10, 40),
        num("ALP", "U/L", 44, 147),
        num("Total bilirubin", "mg/dL", 0.1, 1.2),
    ],
    "Renal Function Test (RFT)": [
        num("Creatinine", "mg/dL", 0.6, 1.3),
        num("BUN", "mg/dL", 7, 20),
        num("Uric acid", "mg/dL", 3.5, 7.2),
    ],
    "Lipid Profile": [
        num("Total cholesterol", "mg/dL", None, 200),
        num("Triglycerides", "mg/dL", None, 150),
        num("HDL", "mg/dL", 40, None),
        num("LDL", "mg/dL", None, 100),
    ],
    "ESR": [num("ESR", "mm/hr", 0, 20)],
    "CRP": [num("CRP", "mg/L", None, 6)],
}


def _d(v):
    return None if v is None else Decimal(str(v))


class Command(BaseCommand):
    help = "Seed structured lab test parameters into clinic service catalogs."

    def add_arguments(self, parser):
        parser.add_argument("--tin")
        parser.add_argument("--branch")

    def handle(self, *args, **opts):
        scopes = BillableService.objects.values_list("clinic_tin", "branch_name").distinct()
        if opts["tin"]:
            scopes = [(opts["tin"], opts["branch"] or "")]
        attached = created = 0
        for tin, branch in scopes:
            with transaction.atomic():
                services = BillableService.objects.filter(
                    clinic_tin=tin, branch_name__iexact=branch, service_type="lab"
                )
                by_name = {s.name.strip().lower(): s for s in services}
                for name, params in TEMPLATES.items():
                    service = by_name.get(name.lower())
                    if service is None:
                        code = "LAB-" + "".join(w[0] for w in name.replace("(", " ").replace(")", " ").split()).upper()
                        if BillableService.objects.filter(clinic_tin=tin, branch_name__iexact=branch, code=code).exists():
                            code = f"{code}-{BillableService.objects.filter(clinic_tin=tin).count()}"
                        service = BillableService.objects.create(
                            clinic_tin=tin, branch_name=branch, code=code, name=name,
                            department="Laboratory", service_type="lab",
                            unit_price=Decimal("0"), is_active=False,
                        )
                        created += 1
                    if service.parameters.exists():
                        continue
                    LabTestParameter.objects.bulk_create([
                        LabTestParameter(
                            service=service, sort_order=i,
                            **{k: (_d(v) if k.startswith(("ref_", "critical_")) else v) for k, v in p.items()},
                        )
                        for i, p in enumerate(params)
                    ])
                    attached += 1
        self.stdout.write(self.style.SUCCESS(
            f"Attached parameters to {attached} services; created {created} inactive services."
        ))
