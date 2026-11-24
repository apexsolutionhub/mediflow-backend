"""Seed a starter medicine catalog (with suppliers) for a clinic.

Usage:
  python manage.py seed_medicines                      # clinic of user "apex", every branch it has
  python manage.py seed_medicines --username apex
  python manage.py seed_medicines --tin 123 --branch Main
  python manage.py seed_medicines --replace            # wipe this branch's medicines first

Some lines are sold only by the whole pack; others also loose (tablets, ampoules, ml...).
Re-running skips names that already exist (same name + batch), so it is safe to repeat.
"""
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from clinic import ledger
from clinic.models import Medicine, Supplier

SUPPLIERS = [
    ("EPHARM Wholesale", "0111550101", "orders@epharm.example"),
    ("Addis Pharma Depot", "0911223344", "sales@addispharma.example"),
    ("Cadila Distribution", "0922334455", ""),
]

# name, category, supplier idx, pack unit, packs, pack price, cost, loose (size, unit, price|None), expiry months, batch, paid fraction, description
M = [
    ("Paracetamol 500mg", "pain_fever", 0, "box", 40, "120", "85", (100, "Tablet", "1.50"), 24, "PCM-2601", 1, "Box of 100 tablets"),
    ("Ibuprofen 400mg", "pain_fever", 0, "box", 25, "180", "130", (100, "Tablet", "2.00"), 20, "IBU-2512", 1, "Box of 100 tablets"),
    ("Diclofenac 50mg", "pain_fever", 1, "box", 15, "160", "110", (100, "Tablet", None), 18, "DIC-2602", 1, "Box of 100 tablets"),
    ("Amoxicillin 500mg", "antibiotics", 0, "box", 30, "280", "200", (100, "Capsule", "3.50"), 22, "AMX-2603", 1, "Box of 100 capsules"),
    ("Azithromycin 500mg", "antibiotics", 1, "box", 12, "420", "310", (30, "Tablet", "16.00"), 24, "AZI-2511", 0.5, "Box of 30 tablets"),
    ("Ciprofloxacin 500mg", "antibiotics", 1, "box", 14, "300", "220", (100, "Tablet", None), 20, "CIP-2604", 1, "Box of 100 tablets"),
    ("Ceftriaxone 1g Injection", "antibiotics", 2, "box", 10, "650", "480", (10, "Ampoule", "70.00"), 18, "CEF-2605", 1, "Box of 10 vials"),
    ("Artemether/Lumefantrine 20/120", "malaria", 2, "box", 20, "240", "170", (24, "Tablet", "11.00"), 24, "ALU-2606", 1, "Treatment pack, 24 tablets"),
    ("Albendazole 400mg", "deworming", 0, "box", 18, "90", "60", None, 30, "ALB-2607", 1, "Single-dose chewable, sold per box"),
    ("ORS Sachet", "diarrhea_ors", 0, "box", 35, "150", "100", (25, "Sachet", "7.00"), 30, "ORS-2608", 1, "Box of 25 sachets"),
    ("Omeprazole 20mg", "stomach_acid_ulcer", 1, "box", 16, "220", "150", (30, "Capsule", "8.00"), 22, "OME-2609", 1, "Box of 30 capsules"),
    ("Cetirizine 10mg", "allergy", 2, "box", 20, "110", "70", (30, "Tablet", "4.00"), 26, "CET-2610", 1, "Box of 30 tablets"),
    ("Cough Syrup 100ml", "cough_cold", 0, "piece", 45, "95", "65", None, 18, "CSY-2611", 1, "Bottle, sold whole"),
    ("Salbutamol Inhaler", "asthma", 1, "piece", 12, "260", "190", None, 24, "SAL-2612", 0.6, "200 doses, sold whole"),
    ("Amlodipine 5mg", "hypertension", 2, "box", 14, "180", "120", (30, "Tablet", "6.50"), 24, "AML-2613", 1, "Box of 30 tablets"),
    ("Metformin 500mg", "diabetes", 0, "box", 18, "150", "100", (100, "Tablet", "1.80"), 24, "MET-2614", 1, "Box of 100 tablets"),
    ("Combined Oral Contraceptive", "family_planning", 1, "packet", 30, "60", "40", None, 30, "COC-2615", 1, "Cycle pack, sold whole"),
    ("Folic Acid 5mg", "pregnancy", 2, "box", 12, "70", "45", (100, "Tablet", "1.00"), 28, "FOL-2616", 1, "Box of 100 tablets"),
    ("Clotrimazole Cream 1%", "fungal", 0, "piece", 25, "85", "55", None, 24, "CLO-2617", 1, "20g tube, sold whole"),
    ("Hydrocortisone Cream 1%", "skin", 1, "piece", 18, "75", "48", None, 24, "HYD-2618", 1, "15g tube, sold whole"),
    ("Tetracycline Eye Ointment", "eye_ear", 2, "piece", 22, "45", "28", None, 20, "TET-2619", 1, "Tube, sold whole"),
    ("Multivitamin", "vitamins", 0, "box", 15, "200", "140", (30, "Tablet", "7.50"), 26, "MUL-2620", 1, "Box of 30 tablets"),
    ("Vitamin C 500mg", "vitamins", 2, "box", 3, "130", "90", (100, "Tablet", "1.50"), 20, "VTC-2621", 1, "Box of 100 tablets (low stock)"),
    ("Diazepam Injection 10mg", "other", 1, "box", 8, "180", "125", (10, "Ampoule", "20.00"), 16, "DZP-2622", 1, "Box of 10 ampoules"),
    # Same names from other suppliers/batches, so grouped lists and batch choice can be tried.
    ("Paracetamol 500mg", "pain_fever", 1, "box", 20, "125", "90", (100, "Tablet", "1.60"), 12, "PCM-2702", 1, "Box of 100 tablets"),
    ("Paracetamol 500mg", "pain_fever", 2, "box", 10, "118", "82", (100, "Tablet", "1.50"), 30, "PCM-2703", 1, "Box of 100 tablets"),
    ("Amoxicillin 500mg", "antibiotics", 2, "box", 12, "290", "205", (100, "Capsule", "3.60"), 14, "AMX-2704", 1, "Box of 100 capsules"),
    ("Normal Saline 500ml", "other", 0, "piece", 40, "75", "50", None, 24, "NSL-2623", 1, "IV bag, sold whole"),
]


class Command(BaseCommand):
    help = "Seed a starter medicine catalog and suppliers for a clinic."

    def add_arguments(self, parser):
        parser.add_argument("--username", default="apex", help="Seed the clinic this user belongs to.")
        parser.add_argument("--tin", help="Clinic TIN (overrides --username).")
        parser.add_argument("--branch", help="Only this branch (default: every branch of the clinic).")
        parser.add_argument("--replace", action="store_true", help="Delete existing medicines of the target first.")

    def handle(self, *args, **opts):
        targets = self._targets(opts)
        for tin, branch in targets:
            self._seed(tin, branch, opts["replace"])

    def _targets(self, opts):
        if opts.get("tin"):
            return [(opts["tin"], opts.get("branch") or "Main")]
        User = get_user_model()
        user = User.objects.filter(username=opts["username"]).select_related("profile").first()
        profile = getattr(user, "profile", None) if user else None
        if not profile or not profile.clinic_tin:
            raise CommandError(f'No clinic found for user "{opts["username"]}". Use --tin/--branch.')
        branches = set(
            type(profile).objects.filter(clinic_tin=profile.clinic_tin).values_list("branch_name", flat=True)
        )
        branches = {(b or "").strip() or "Main" for b in branches}
        if opts.get("branch"):
            branches = {opts["branch"]}
        return [(profile.clinic_tin, b) for b in sorted(branches)]

    @transaction.atomic
    def _seed(self, tin, branch, replace):
        if replace:
            Medicine.objects.filter(clinic_tin=tin, branch_name__iexact=branch).delete()
        suppliers = []
        for name, phone, email in SUPPLIERS:
            sup, _ = Supplier.objects.get_or_create(
                clinic_tin=tin,
                branch_name=branch,
                supplier_name=name,
                defaults={"supplier_phone": phone, "supplier_email": email},
            )
            suppliers.append(sup)

        today = date.today()
        created = 0
        for (name, cat, sidx, unit, qty, price, cost, loose, months, batch, paid_frac, desc) in M:
            if Medicine.objects.filter(
                clinic_tin=tin, branch_name__iexact=branch, name=name, batch_number=batch
            ).exists():
                continue
            cost_d, price_d = Decimal(cost), Decimal(price)
            size, loose_unit, loose_price = (loose[0], loose[1], Decimal(loose[2]) if loose[2] else None) if loose else (1, "", None)
            total = cost_d * qty
            medicine = Medicine.objects.create(
                clinic_tin=tin,
                branch_name=branch,
                supplier=suppliers[sidx],
                name=name,
                description=desc,
                category=cat,
                batch_number=batch,
                expiry_date=today + timedelta(days=30 * months),
                unit_of_measure=unit,
                on_hand=qty,
                received_quantity=qty,
                min_threshold=5,
                unit_price=price_d,
                cost=cost_d,
                pack_size=size,
                loose_unit=loose_unit,
                loose_price=loose_price,
                loose_stock=0,
                paid_amount=(total * Decimal(str(paid_frac))).quantize(Decimal("0.01")),
            )
            ledger.record_movement(medicine, "opening", "seed")
            created += 1
        self.stdout.write(
            self.style.SUCCESS(f"{tin} / {branch}: {created} medicines added, {len(suppliers)} suppliers ready.")
        )
