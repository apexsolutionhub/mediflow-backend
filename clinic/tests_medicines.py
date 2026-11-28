"""Medicine registration (pharmacy fields), roles, tenancy, batch, suppliers, loose selling."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from api.models import UserProfile
from clinic import stock
from clinic.models import ClinicalOrder, Encounter, Medicine, Patient, Supplier

TIN = "TEST-TIN-1"
OTHER_TIN = "OTHER-TIN-9"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def _line(supplier, **over):
    base = {
        "name": "Amoxicillin 500mg",
        "category": "antibiotics",
        "supplier_id": supplier.id,
        "unit_price": "12.50",
        "cost": "8.00",
        "on_hand": 10,
        "unit_of_measure": "box",
        "batch_number": "B-100",
        "expiry_date": "2030-01-31",
        "description": "Capsules",
    }
    base.update(over)
    return base


def _make_user(name, role, tin=TIN):
    user = get_user_model().objects.create_user(name, password="x")
    UserProfile.objects.create(user=user, clinic_tin=tin, role=role, branch_name="Main")
    return user


class StockEngineTests(APITestCase):
    def med(self, **kw):
        base = dict(
            clinic_tin=TIN, branch_name="Main", name="Para", on_hand=2, unit_price=20,
            pack_size=10, loose_unit="tablet", loose_stock=0, unit_of_measure="box",
        )
        base.update(kw)
        return Medicine(**base)

    def test_loose_sale_uses_opened_pack_then_opens_next(self):
        m = self.med(on_hand=2, loose_stock=3)
        stock.deduct(m, 2, stock.LOOSE)
        self.assertEqual((m.on_hand, m.loose_stock), (2, 1))
        stock.deduct(m, 12, stock.LOOSE)  # 1 loose left, needs 11 more -> opens 2 packs
        self.assertEqual((m.on_hand, m.loose_stock), (0, 9))

    def test_cannot_oversell_either_unit(self):
        m = self.med(on_hand=1, loose_stock=4)
        with self.assertRaises(stock.StockError):
            stock.deduct(m, 15, stock.LOOSE)
        with self.assertRaises(stock.StockError):
            stock.deduct(m, 2, stock.PACK)

    def test_loose_not_allowed_without_pack_size(self):
        with self.assertRaises(stock.StockError):
            stock.deduct(self.med(pack_size=1), 1, stock.LOOSE)

    def test_restock_text_prices_and_capacity(self):
        m = self.med(on_hand=1, loose_stock=8)
        stock.restock(m, 5, stock.LOOSE)
        self.assertEqual((m.on_hand, m.loose_stock), (2, 3))
        self.assertEqual(stock.stock_text(m), "2 boxes + 3 tablets")
        self.assertEqual(stock.loose_unit_price(m), Decimal("2.00"))
        self.assertEqual(stock.capacity(m, stock.LOOSE), 23)

    def test_last_pack_sold_loose_still_reads_as_in_stock(self):
        m = self.med(on_hand=1, loose_stock=0)
        stock.deduct(m, 3, stock.LOOSE)
        self.assertEqual((m.on_hand, m.loose_stock), (0, 7))
        self.assertEqual(stock.stock_text(m), "7 tablets")


@mock.patch(PERM, return_value=True)
class MedicineRegistrationTests(APITestCase):
    def setUp(self):
        self.mgr = _make_user("mmgr", "manager")
        self.doc = _make_user("mdoc", "doctor")
        self.phar = _make_user("mphar", "pharmacist")
        self.other_mgr = _make_user("omgr", "manager", OTHER_TIN)
        self.sup = Supplier.objects.create(
            clinic_tin=TIN, branch_name="Main", supplier_name="Addis Pharma", supplier_phone="0911"
        )
        self.other_sup = Supplier.objects.create(
            clinic_tin=OTHER_TIN, branch_name="Main", supplier_name="Foreign Ltd", supplier_phone="0922"
        )

    def post(self, body, user=None):
        self.client.force_authenticate(user or self.mgr)
        return self.client.post("/api/clinic/medicines/", body, format="json")

    # ----- the registered fields and what gets computed -----
    def test_registers_all_fields_and_computes_totals(self, _perm):
        res = self.post(_line(self.sup))
        self.assertEqual(res.status_code, 201, res.data)
        d = res.data
        self.assertEqual(
            (d["name"], d["category"], d["unit_of_measure"], d["batch_number"]),
            ("Amoxicillin 500mg", "antibiotics", "box", "B-100"),
        )
        self.assertEqual(d["supplier_name"], "Addis Pharma")
        self.assertEqual((d["received_quantity"], d["total_cost"]), (10, "80.00"))
        self.assertEqual(
            (d["paid_amount"], d["credit_amount"], d["payment_status"]), ("80.00", "0.00", "paid")
        )
        self.assertEqual((d["pack_size"], d["loose_unit"], d["loose_stock"]), (1, "", 0))

    def test_partial_payment_means_supplier_credit(self, _perm):
        d = self.post(_line(self.sup, paid_amount="30.00")).data
        self.assertEqual((d["credit_amount"], d["payment_status"]), ("50.00", "credit"))

    def test_paid_cannot_exceed_total_cost(self, _perm):
        res = self.post(_line(self.sup, paid_amount="81.00"))
        self.assertEqual(res.status_code, 400)
        self.assertIn("paid_amount", res.data)

    def test_supplier_batch_and_price_are_required(self, _perm):
        no_sup = _line(self.sup)
        del no_sup["supplier_id"]
        self.assertIn("supplier_id", self.post(no_sup).data)
        self.assertIn("batch_number", self.post(_line(self.sup, batch_number="  ")).data)
        self.assertIn("unit_price", self.post(_line(self.sup, unit_price="0")).data)
        self.assertIn("cost", self.post(_line(self.sup, cost="-1")).data)

    def test_pack_splitting_rules(self, _perm):
        ok = self.post(_line(self.sup, pack_size=10, loose_unit="tablet", loose_price="1.50")).data
        self.assertEqual(
            (ok["pack_size"], ok["loose_unit"], ok["loose_unit_price"]), (10, "tablet", "1.50")
        )
        derived = self.post(_line(self.sup, name="Beta", pack_size=10, loose_unit="tablet")).data
        self.assertEqual(derived["loose_unit_price"], "1.25")  # 12.50 / 10
        bad = self.post(
            _line(self.sup, name="Gamma", pack_size=10, loose_unit="tablet", loose_stock=10)
        )
        self.assertIn("loose_stock", bad.data)
        single = self.post(
            _line(self.sup, name="Delta", pack_size=1, loose_unit="tablet", loose_price="2")
        ).data
        self.assertEqual((single["loose_unit"], single["loose_price"]), ("", None))

    def test_editing_quantity_moves_received_quantity(self, _perm):
        mid = self.post(_line(self.sup)).data["id"]
        self.client.force_authenticate(self.mgr)
        res = self.client.patch(f"/api/clinic/medicines/{mid}/", {"on_hand": 15}, format="json")
        self.assertEqual((res.data["received_quantity"], res.data["total_cost"]), (15, "120.00"))

    # ----- roles -----
    def test_only_manager_can_write_but_clinic_staff_can_read(self, _perm):
        mid = self.post(_line(self.sup)).data["id"]
        for user in (self.doc, self.phar):
            self.assertEqual(self.post(_line(self.sup, name="Xylo Med"), user).status_code, 403)
            patch = self.client.patch(
                f"/api/clinic/medicines/{mid}/", {"unit_price": "1"}, format="json"
            )
            self.assertEqual(patch.status_code, 403)
            self.assertEqual(self.client.delete(f"/api/clinic/medicines/{mid}/").status_code, 403)
            self.assertEqual(self.client.get("/api/clinic/medicines/").status_code, 200)
            batch = self.client.post("/api/clinic/medicines/batch/", {"items": []}, format="json")
            self.assertEqual(batch.status_code, 403)
        self.assertEqual(Medicine.objects.get(pk=mid).unit_price, Decimal("12.50"))

    # ----- tenancy -----
    def test_other_clinics_supplier_and_medicines_are_invisible(self, _perm):
        res = self.post(_line(self.other_sup))
        self.assertEqual(res.status_code, 400)
        self.assertIn("supplier_id", res.data)
        mid = self.post(_line(self.sup)).data["id"]
        self.client.force_authenticate(self.other_mgr)
        self.assertEqual(self.client.get(f"/api/clinic/medicines/{mid}/").status_code, 404)
        self.assertEqual(self.client.get("/api/clinic/medicines/").data["count"], 0)
        self.assertEqual(self.client.get("/api/clinic/suppliers/").data["count"], 1)

    # ----- batch: all or nothing -----
    def test_batch_saves_all_lines(self, _perm):
        self.client.force_authenticate(self.mgr)
        res = self.client.post(
            "/api/clinic/medicines/batch/",
            {"items": [_line(self.sup, name="Alpha"), _line(self.sup, name="Beta", batch_number="B-2")]},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(res.data), 2)
        self.assertEqual(Medicine.objects.filter(clinic_tin=TIN).count(), 2)

    def test_batch_with_one_bad_line_saves_nothing_so_a_retry_cannot_duplicate(self, _perm):
        self.client.force_authenticate(self.mgr)
        res = self.client.post(
            "/api/clinic/medicines/batch/",
            {
                "items": [
                    _line(self.sup, name="Good"),
                    _line(self.sup, name="Bad", unit_price="0"),
                    _line(self.sup, name="Also good", batch_number="B-3"),
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual([e["line"] for e in res.data["errors"]], [2])
        self.assertEqual(res.data["errors"][0]["name"], "Bad")
        self.assertEqual(Medicine.objects.filter(clinic_tin=TIN).count(), 0)

    # ----- deleting -----
    def test_cannot_delete_a_line_that_still_owes_the_supplier(self, _perm):
        mid = self.post(_line(self.sup, paid_amount="10")).data["id"]
        self.client.force_authenticate(self.mgr)
        res = self.client.delete(f"/api/clinic/medicines/{mid}/")
        self.assertEqual(res.status_code, 400)
        self.assertIn("owed", res.data["detail"])
        self.client.patch(f"/api/clinic/medicines/{mid}/", {"paid_amount": "80"}, format="json")
        self.assertEqual(self.client.delete(f"/api/clinic/medicines/{mid}/").status_code, 204)

    # ----- suppliers -----
    def test_supplier_crud_rules(self, _perm):
        self.client.force_authenticate(self.mgr)
        body = {"supplier_name": "Zeta", "supplier_phone": "0933"}
        ok = self.client.post("/api/clinic/suppliers/", body, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        dup = self.client.post(
            "/api/clinic/suppliers/", {**body, "supplier_name": "zeta"}, format="json"
        )
        self.assertEqual(dup.status_code, 400)
        self.post(_line(self.sup))
        self.assertEqual(self.client.delete(f"/api/clinic/suppliers/{self.sup.id}/").status_code, 400)
        self.assertEqual(self.client.delete(f"/api/clinic/suppliers/{ok.data['id']}/").status_code, 204)
        self.client.force_authenticate(self.doc)
        denied = self.client.post(
            "/api/clinic/suppliers/", {"supplier_name": "Q", "supplier_phone": "09"}, format="json"
        )
        self.assertEqual(denied.status_code, 403)


@mock.patch(PERM, return_value=True)
class PrescribeAndDispenseLooseTests(APITestCase):
    """The doctor orders a quantity in packs or loose units; the pharmacist's dispense matches it."""

    def setUp(self):
        self.doc = _make_user("ldoc", "doctor")
        self.phar = _make_user("lphar", "pharmacist")
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="L1", full_name="P")
        self.enc = Encounter.objects.create(
            clinic_tin=TIN, branch_name="Main", number="L-1", patient=patient, arrival_type="new"
        )
        self.med = Medicine.objects.create(
            clinic_tin=TIN, branch_name="Main", name="Paracetamol 500mg", on_hand=5, unit_price=20,
            pack_size=10, loose_unit="tablet", loose_price=Decimal("2.50"), unit_of_measure="box",
            min_threshold=0,  # these tests are about pricing and quantity, not the low-stock alert level
        )
        self.plain = Medicine.objects.create(
            clinic_tin=TIN, branch_name="Main", name="Saline", on_hand=3, unit_price=40, min_threshold=0
        )

    def order(self, medicine, **body):
        self.client.force_authenticate(self.doc)
        payload = {
            "encounter": self.enc.id,
            "order_type": "prescription",
            "details": "x",
            "medicine": medicine.id,
        }
        payload.update(body)
        return self.client.post("/api/clinic/orders/", payload, format="json")

    def test_loose_order_is_priced_per_tablet_and_billed_by_quantity(self, _perm):
        res = self.order(self.med, quantity=12, sale_unit="loose")
        self.assertEqual(res.status_code, 201, res.data)
        order = ClinicalOrder.objects.get(pk=res.data["id"])
        self.assertEqual((order.quantity, order.sale_unit), (12, "loose"))
        self.assertEqual((order.billable.quantity, order.billable.unit_price), (12, Decimal("2.50")))

    def test_pack_order_defaults_and_pricing(self, _perm):
        order = ClinicalOrder.objects.get(pk=self.order(self.med).data["id"])
        self.assertEqual(
            (order.quantity, order.sale_unit, order.billable.unit_price), (1, "pack", Decimal("20.00"))
        )
        two = ClinicalOrder.objects.get(pk=self.order(self.plain, quantity=2).data["id"])
        self.assertEqual((two.billable.quantity, two.billable.unit_price), (2, Decimal("40.00")))

    def test_bad_orders_are_rejected(self, _perm):
        self.assertEqual(self.order(self.plain, quantity=1, sale_unit="loose").status_code, 400)
        self.assertEqual(self.order(self.med, quantity=0).status_code, 400)
        self.assertEqual(self.order(self.med, quantity="abc").status_code, 400)
        self.assertEqual(ClinicalOrder.objects.count(), 0)

    def test_more_than_the_clinic_has_goes_to_an_external_pharmacy(self, _perm):
        # 5 packs in stock and no reserve: 6 packs is 5 from the clinic and 1 outside, not an error.
        res = self.order(self.med, quantity=6)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["stock_split"]["clinic_quantity"], res.data["stock_split"]["external_quantity"]), (5, 1))
        self.assertEqual(ClinicalOrder.objects.count(), 2)

    def test_dispense_loose_opens_packs_and_matches_the_order(self, _perm):
        oid = self.order(self.med, quantity=12, sale_unit="loose").data["id"]
        ClinicalOrder.objects.filter(pk=oid).update(status="PaymentApproved")
        self.client.force_authenticate(self.phar)
        before = self.client.get(f"/api/clinic/orders/{oid}/").data
        self.assertEqual(
            (before["dispense_quantity"], before["sale_unit"], before["medicine_can_dispense"]),
            (12, "loose", True),
        )
        self.assertEqual(
            (before["medicine_stock_text"], before["medicine_stock_after_text"]),
            ("5 boxes", "3 boxes + 8 tablets"),
        )
        res = self.client.post(
            f"/api/clinic/orders/{oid}/dispense/",
            {"medicine": self.plain.id, "quantity": 99},  # ignored
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.med.refresh_from_db()
        self.plain.refresh_from_db()
        self.assertEqual((self.med.on_hand, self.med.loose_stock, self.plain.on_hand), (3, 8, 3))


@mock.patch(PERM, return_value=True)
class DamageAndActiveTests(APITestCase):
    def setUp(self):
        self.mgr = _make_user("dmgr", "manager")
        self.phar = _make_user("dphar", "pharmacist")
        sup = Supplier.objects.create(
            clinic_tin=TIN, branch_name="Main", supplier_name="S1", supplier_phone="0911"
        )
        self.med = Medicine.objects.create(
            clinic_tin=TIN, branch_name="Main", supplier=sup, name="Paracetamol", batch_number="B1",
            unit_of_measure="box", on_hand=5, unit_price=100, cost=60, pack_size=10, loose_unit="Tablet",
            received_quantity=5, paid_amount=300,
        )

    def damage(self, **body):
        self.client.force_authenticate(self.mgr)
        payload = {"medicine": self.med.id, "quantity": 1, "sale_unit": "pack", "reason": "Wet"}
        payload.update(body)
        return self.client.post("/api/clinic/medicine-damages/", payload, format="json")

    def test_damage_deducts_packs_and_loose(self, _perm):
        self.assertEqual(self.damage(quantity=1).status_code, 201)
        self.med.refresh_from_db()
        self.assertEqual((self.med.on_hand, self.med.loose_stock), (4, 0))
        self.assertEqual(self.damage(quantity=3, sale_unit="loose").status_code, 201)
        self.med.refresh_from_db()
        self.assertEqual((self.med.on_hand, self.med.loose_stock), (3, 7))

    def test_cannot_damage_more_than_in_stock(self, _perm):
        res = self.damage(quantity=6)
        self.assertEqual(res.status_code, 400)
        self.med.refresh_from_db()
        self.assertEqual(self.med.on_hand, 5)

    def test_edit_adjusts_and_delete_restores(self, _perm):
        entry = self.damage(quantity=2).json()
        self.client.force_authenticate(self.mgr)
        res = self.client.patch(
            f"/api/clinic/medicine-damages/{entry['id']}/", {"quantity": 4}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.med.refresh_from_db()
        self.assertEqual(self.med.on_hand, 1)
        res = self.client.delete(f"/api/clinic/medicine-damages/{entry['id']}/")
        self.assertEqual(res.status_code, 204)
        self.med.refresh_from_db()
        self.assertEqual(self.med.on_hand, 5)

    def test_failed_edit_keeps_stock(self, _perm):
        entry = self.damage(quantity=2).json()
        res = self.client.patch(
            f"/api/clinic/medicine-damages/{entry['id']}/", {"quantity": 9}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.med.refresh_from_db()
        self.assertEqual(self.med.on_hand, 3)

    def test_only_manager_records_damage(self, _perm):
        self.client.force_authenticate(self.phar)
        res = self.client.post(
            "/api/clinic/medicine-damages/", {"medicine": self.med.id, "quantity": 1}, format="json"
        )
        self.assertEqual(res.status_code, 403)

    def test_finished_stock_turns_inactive_and_restock_reactivates(self, _perm):
        self.assertTrue(self.med.is_active)
        self.assertEqual(self.damage(quantity=5).status_code, 201)
        self.med.refresh_from_db()
        self.assertFalse(self.med.is_active)
        entry_id = self.client.get("/api/clinic/medicine-damages/", {"medicine": self.med.id}).json()
        rows = entry_id["results"] if isinstance(entry_id, dict) else entry_id
        self.client.delete(f"/api/clinic/medicine-damages/{rows[0]['id']}/")
        self.med.refresh_from_db()
        self.assertTrue(self.med.is_active)
