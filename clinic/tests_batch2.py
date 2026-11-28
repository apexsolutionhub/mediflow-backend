"""Batch 2: duplicate guards, allergies on the chart, low-stock ordering, follow-up edits."""
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import (
    Appointment,
    BillableService,
    ClinicalOrder,
    Department,
    DoctorChart,
    Encounter,
    Medicine,
    Patient,
    Supplier,
)

TIN = "B2-TIN"
BRANCH = "Main"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="pw-123456")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name=BRANCH)
    return user


class Base(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager = _user("b2mgr", "manager")
        self.doctor = _user("b2doc", "doctor")
        self.api = APIClient()
        self.api.force_authenticate(self.manager)
        self.doc_api = APIClient()
        self.doc_api.force_authenticate(self.doctor)

    def patient(self, name="Hana Bekele", phone="0911000111", **kw):
        return Patient.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, mrn=f"MRN-{Patient.objects.count() + 1:05d}", full_name=name, age=30,
            gender="Female", phone=phone, **kw
        )

    def visit(self, patient, status=Encounter.STATUS_ACTIVE):
        n = Encounter.objects.count() + 1
        return Encounter.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, number=f"ENC-T-{n:04d}", patient=patient, arrival_type="new", status=status
        )

    def supplier(self, name="Alpha Supplies"):
        return Supplier.objects.create(clinic_tin=TIN, branch_name=BRANCH, supplier_name=name, supplier_phone="0911")


class RegistrationGuardTests(Base):
    def reg(self, **over):
        body = {"full_name": "Hana Bekele", "age": 30, "gender": "Female", "phone": "0911000111"}
        body.update(over)
        return self.api.post("/api/clinic/patients/", body, format="json")

    def test_a_patient_with_an_open_visit_cannot_be_registered_again(self):
        p = self.patient()
        visit = self.visit(p, Encounter.STATUS_OPEN)
        res = self.reg()
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual((body["code"], body["open_encounter"], body["patient_id"]), ("patient_exists", visit.number, p.id))
        self.assertEqual(Patient.objects.count(), 1)

    def test_a_patient_with_history_is_sent_to_returning_registration(self):
        p = self.patient()
        self.visit(p, Encounter.STATUS_CLOSED)
        res = self.reg(full_name="  hana   BEKELE ")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "patient_exists")
        self.assertIsNone(res.json()["open_encounter"])
        self.assertIn("Returning registration", res.json()["detail"])

    def test_the_same_name_with_a_different_phone_is_a_new_person(self):
        self.patient()
        self.assertEqual(self.reg(phone="0922999888").status_code, 201)

    def test_no_phone_matches_on_name_age_and_gender(self):
        self.patient(phone="")
        self.assertEqual(self.reg(phone="").status_code, 409)
        self.assertEqual(self.reg(phone="", age=44).status_code, 201)

    def test_a_second_open_visit_is_refused(self):
        p = self.patient()
        self.visit(p, Encounter.STATUS_ACTIVE)
        res = self.api.post("/api/clinic/encounters/", {"patient_id": p.id, "arrival_type": "returning"}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "open_encounter")

    def test_a_checked_out_patient_can_start_a_new_visit(self):
        p = self.patient()
        self.visit(p, Encounter.STATUS_CLOSED)
        res = self.api.post("/api/clinic/encounters/", {"patient_id": p.id, "arrival_type": "returning"}, format="json")
        self.assertEqual(res.status_code, 201, res.content)


class CatalogueGuardTests(Base):
    def test_a_duplicate_supplier_is_refused_on_create_and_rename(self):
        self.supplier("Alpha Supplies")
        other = self.supplier("Beta Supplies")
        res = self.api.post("/api/clinic/suppliers/", {"supplier_name": "alpha supplies", "supplier_phone": "0912"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.api.patch(f"/api/clinic/suppliers/{other.id}/", {"supplier_name": "ALPHA SUPPLIES"}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_a_duplicate_department_is_refused(self):
        Department.objects.create(clinic_tin=TIN, branch_name=BRANCH, name="Laboratory")
        other = Department.objects.create(clinic_tin=TIN, branch_name=BRANCH, name="Imaging")
        self.assertEqual(self.api.post("/api/clinic/departments/", {"name": "laboratory"}, format="json").status_code, 400)
        self.assertEqual(self.api.patch(f"/api/clinic/departments/{other.id}/", {"name": "Laboratory"}, format="json").status_code, 400)
        self.assertEqual(self.api.post("/api/clinic/departments/", {"name": "Pharmacy"}, format="json").status_code, 201)

    def test_an_exact_copy_of_a_billable_service_is_refused(self):
        body = {
            "code": "CBC", "name": "Complete blood count", "department": "Laboratory", "service_type": "lab",
            "unit_price": "250.00", "default_quantity": 1,
        }
        self.assertEqual(self.api.post("/api/clinic/services/", body, format="json").status_code, 201)
        res = self.api.post("/api/clinic/services/", body, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("exactly the same", str(res.json()))
        # The same code is refused too; a different code and price is a different service.
        self.assertEqual(self.api.post("/api/clinic/services/", {**body, "unit_price": "300.00"}, format="json").status_code, 400)
        self.assertEqual(
            self.api.post("/api/clinic/services/", {**body, "code": "CBC2", "unit_price": "300.00"}, format="json").status_code, 201
        )


class MedicineGuardTests(Base):
    def body(self, supplier, **over):
        body = {
            "supplier_id": supplier.id, "name": "Amoxicillin 500", "category": "other", "unit_price": "12.00",
            "cost": "8.00", "on_hand": 20, "batch_number": "B-1", "expiry_date": str(date.today() + timedelta(days=400)),
            "unit_of_measure": "box", "min_threshold": 4,
        }
        body.update(over)
        return body

    def test_an_exact_copy_is_refused(self):
        s = self.supplier()
        self.assertEqual(self.api.post("/api/clinic/medicines/", self.body(s), format="json").status_code, 201)
        res = self.api.post("/api/clinic/medicines/", self.body(s), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("exactly like this", str(res.json()))

    def test_the_same_name_is_warned_about_then_saved_when_confirmed(self):
        s = self.supplier()
        self.api.post("/api/clinic/medicines/", self.body(s), format="json")
        again = self.body(s, batch_number="B-2")
        res = self.api.post("/api/clinic/medicines/", again, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "duplicate_warning")
        self.assertEqual(Medicine.objects.count(), 1)
        res = self.api.post("/api/clinic/medicines/", {**again, "confirm_duplicate": True}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Medicine.objects.count(), 2)

    def test_a_batch_warns_once_and_refuses_repeated_lines(self):
        s = self.supplier()
        self.api.post("/api/clinic/medicines/", self.body(s), format="json")
        lines = [self.body(s, batch_number="B-2"), self.body(s, name="Paracetamol", batch_number="P-1")]
        res = self.api.post("/api/clinic/medicines/batch/", {"items": lines}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "duplicate_warning")
        res = self.api.post("/api/clinic/medicines/batch/", {"items": lines, "confirm_duplicate": True}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        twin = [self.body(s, name="Ibuprofen", batch_number="I-1")] * 2
        res = self.api.post("/api/clinic/medicines/batch/", {"items": twin}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("repeats line 1", str(res.json()))


class EquipmentWarningTests(Base):
    def test_an_identical_open_request_is_warned_about(self):
        body = {"title": "X-ray tube noise", "details": "Loud click on exposure"}
        self.assertEqual(self.api.post("/api/clinic/tickets/", body, format="json").status_code, 201)
        res = self.api.post("/api/clinic/tickets/", body, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "duplicate_warning")
        res = self.api.post("/api/clinic/tickets/", {**body, "confirm_duplicate": True}, format="json")
        self.assertEqual(res.status_code, 201)


class OrderTests(Base):
    def setUp(self):
        super().setUp()
        self.p = self.patient()
        self.enc = self.visit(self.p)
        self.service = BillableService.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, code="CBC", name="CBC", department="Laboratory",
            service_type="lab", unit_price=250,
        )

    def order(self, **over):
        body = {"encounter": self.enc.id, "order_type": "lab", "details": "CBC", "service": self.service.id}
        body.update(over)
        return self.doc_api.post("/api/clinic/orders/", body, format="json")

    def test_a_repeated_lab_request_is_warned_about(self):
        self.assertEqual(self.order().status_code, 201)
        res = self.order()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "duplicate_warning")
        self.assertEqual(self.order(confirm_duplicate=True).status_code, 201)
        self.assertEqual(ClinicalOrder.objects.filter(order_type="lab").count(), 2)

    def test_the_same_test_for_another_patient_is_not_a_repeat(self):
        self.order()
        other = self.visit(self.patient("Dawit Alemu", "0933"))
        self.assertEqual(self.order(encounter=other.id).status_code, 201)

    def medicine(self, on_hand=10, threshold=4, **kw):
        s = self.supplier()
        return Medicine.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, supplier=s, name="Cefalexin", unit_price=10, cost=6, on_hand=on_hand,
            min_threshold=threshold, batch_number="C1", received_quantity=on_hand, **kw
        )

    def rx(self, med, qty, **over):
        body = {"encounter": self.enc.id, "order_type": "prescription", "details": "Take twice a day", "medicine": med.id, "quantity": qty}
        body.update(over)
        return self.doc_api.post("/api/clinic/orders/", body, format="json")

    def test_an_order_within_the_alert_level_all_comes_from_the_clinic(self):
        med = self.medicine()
        res = self.rx(med, 5)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertNotIn("stock_split", res.json())
        order = ClinicalOrder.objects.get()
        self.assertEqual((order.fulfillment, order.quantity), (ClinicalOrder.FULFILLMENT_CLINIC, 5))

    def test_the_part_above_the_alert_level_becomes_an_external_order(self):
        med = self.medicine()  # 10 in stock, alert level 4: the clinic can give 6
        res = self.rx(med, 8)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.json()["stock_split"]["clinic_quantity"], res.json()["stock_split"]["external_quantity"]), (6, 2))
        clinic = ClinicalOrder.objects.get(fulfillment=ClinicalOrder.FULFILLMENT_CLINIC)
        external = ClinicalOrder.objects.get(fulfillment=ClinicalOrder.FULFILLMENT_EXTERNAL)
        self.assertEqual((clinic.quantity, external.quantity), (6, 2))
        self.assertIsNotNone(clinic.billable)
        self.assertIsNone(external.billable)  # nothing to pay here for what an outside pharmacy sells

    def test_a_medicine_already_at_its_alert_level_goes_entirely_outside(self):
        med = self.medicine(on_hand=4, threshold=4)
        res = self.rx(med, 3)
        self.assertEqual(res.status_code, 201, res.content)
        order = ClinicalOrder.objects.get()
        self.assertEqual(order.fulfillment, ClinicalOrder.FULFILLMENT_EXTERNAL)
        self.assertIsNone(order.billable)
        self.assertEqual(res.json()["stock_split"]["external_quantity"], 3)

    def test_stock_check_tells_the_doctor_before_ordering(self):
        med = self.medicine()
        res = self.doc_api.get("/api/clinic/orders/stock_check/", {"medicine": med.id, "quantity": 8})
        data = res.json()
        self.assertEqual((data["clinic_quantity"], data["external_quantity"], data["low"]), (6, 2, True))
        self.assertIn("low-stock alert level", data["message"])
        calm = self.doc_api.get("/api/clinic/orders/stock_check/", {"medicine": med.id, "quantity": 2}).json()
        self.assertFalse(calm["low"])
        self.assertEqual(calm["message"], "")

    def test_a_repeated_medicine_is_warned_about(self):
        med = self.medicine()
        self.assertEqual(self.rx(med, 1).status_code, 201)
        res = self.rx(med, 1)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.rx(med, 1, confirm_duplicate=True).status_code, 201)


class ChartAndFollowUpTests(Base):
    def test_the_doctor_records_allergies_on_the_chart_and_they_stay_on_the_patient(self):
        p = self.patient()
        enc = self.visit(p)
        body = {"encounter": enc.id, "chief_complaint": "Cough", "allergies": "Penicillin (rash)"}
        res = self.doc_api.post("/api/clinic/charts/", body, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["allergies"], "Penicillin (rash)")
        p.refresh_from_db()
        self.assertEqual(p.allergies, "Penicillin (rash)")
        # A second save edits the same chart; leaving allergies out keeps what is recorded.
        self.doc_api.post("/api/clinic/charts/", {"encounter": enc.id, "chief_complaint": "Cough, worse"}, format="json")
        self.assertEqual(DoctorChart.objects.filter(encounter=enc).count(), 1)
        p.refresh_from_db()
        self.assertEqual(p.allergies, "Penicillin (rash)")

    def follow_up(self, enc, when, reason="Review"):
        return self.doc_api.post(
            "/api/clinic/appointments/",
            {
                "patient": enc.patient_id, "scheduled_at": when.isoformat(), "reason": reason,
                "kind": "follow_up", "encounter_number": enc.number,
            },
            format="json",
        )

    def test_a_second_follow_up_before_the_first_arrives_edits_it(self):
        enc = self.visit(self.patient())
        first = self.follow_up(enc, timezone.now() + timedelta(days=3))
        self.assertEqual(first.status_code, 201, first.content)
        later = timezone.now() + timedelta(days=7)
        second = self.follow_up(enc, later, "Review labs")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertTrue(second.json()["edited_existing"])
        rows = Appointment.objects.filter(patient_id=enc.patient_id)
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().reason, "Review labs")

    def test_after_the_first_follow_up_has_passed_a_new_one_is_a_new_booking(self):
        enc = self.visit(self.patient())
        Appointment.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, patient=enc.patient, scheduled_at=timezone.now() - timedelta(days=1),
            kind="follow_up", encounter_number=enc.number,
        )
        res = self.follow_up(enc, timezone.now() + timedelta(days=2))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(Appointment.objects.filter(patient=enc.patient).count(), 2)

    def test_a_follow_up_in_another_visit_is_refused_while_the_first_is_still_pending(self):
        # Batch 5 rule: until a patient reaches their first appointment they cannot get a second.
        p = self.patient()
        a = self.visit(p, Encounter.STATUS_CLOSED)
        b = self.visit(p, Encounter.STATUS_ACTIVE)
        self.follow_up(a, timezone.now() + timedelta(days=3))
        res = self.follow_up(b, timezone.now() + timedelta(days=4))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "open_appointment")
