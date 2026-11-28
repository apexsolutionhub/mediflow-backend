"""Batch 6: branch-to-branch referrals, medicine expiry rules and Status Treatment."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import (
    BillableItem,
    BillableService,
    ClinicBranch,
    ClinicalOrder,
    Encounter,
    Medicine,
    Patient,
    Referral,
    Supplier,
    TreatmentAssignment,
)

TIN = "B6-TIN"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def make_user(name, role, branch="Main"):
    user = get_user_model().objects.create_user(name, password="pw-123456")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name=branch)
    client = APIClient()
    client.force_authenticate(user)
    return client


class Base(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        ClinicBranch.objects.create(clinic_tin=TIN, name="Main", is_main=True)
        ClinicBranch.objects.create(clinic_tin=TIN, name="East")
        self.doctor = make_user("b6doc", "doctor")
        self.reception = make_user("b6rec", "reception")
        self.manager = make_user("b6mgr", "manager")
        self.nurse = make_user("b6nurse", "nurse")
        self.east_manager = make_user("b6east", "manager", "East")
        self.east_doctor = make_user("b6eastdoc", "doctor", "East")
        self.patient = Patient.objects.create(
            clinic_tin=TIN, branch_name="Main", mrn="M1", full_name="Hana Bekele", age=34, gender="Female",
            phone="0911000111", address="Bole",
        )
        self.enc = Encounter.objects.create(clinic_tin=TIN, branch_name="Main", number="E1", patient=self.patient, arrival_type="new")

    def pay_all(self, enc):
        enc.billables.update(payment_status="PaymentApproved")

    def refer(self, kind="internal", **over):
        body = {
            "encounter": self.enc.id, "destination_kind": kind, "diagnosis": "Suspected angina", "priority": "high",
            "lab_summary": "ECG abnormal",
        }
        if kind == "internal":
            body.update(to_branch="East", to_department="Cardiology")
        else:
            body.update(external_institution="St. Paul Hospital", to_department="Cardiology")
        body.update(over)
        return self.doctor.post("/api/clinic/referrals/", body, format="json")


class ReferralCreateTests(Base):
    def test_branch_to_branch_referral_keeps_priority_and_starts_pending(self):
        res = self.refer()
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual((body["priority"], body["approval_status"]), ("high", "pending"))
        self.assertEqual(body["handover"]["name"], "Hana Bekele")
        self.assertEqual(body["handover"]["sex"], "Female")

    def test_cannot_refer_to_own_branch_or_a_branch_outside_the_clinic(self):
        self.assertEqual(self.refer(to_branch="Main").status_code, 400)
        self.assertEqual(self.refer(to_branch="Nowhere").status_code, 400)

    def test_results_are_sent_only_when_they_exist(self):
        self.assertEqual(self.refer().json()["results"], [])
        svc = BillableService.objects.create(clinic_tin=TIN, branch_name="Main", code="L1", name="CBC", department="Lab", unit_price=100, service_type="lab")
        bill = BillableItem.objects.create(encounter=self.enc, service=svc, description="CBC", department="Lab", unit_price=100)
        ClinicalOrder.objects.create(encounter=self.enc, order_type="lab", status="Completed", details="CBC", billable=bill, result_text="Hb 11")
        ClinicalOrder.objects.create(encounter=self.enc, order_type="radiology", status="PaymentApproved", details="Chest X-ray")
        out = self.refer(to_department="Other").json()["results"]
        self.assertEqual([r["name"] for r in out], ["CBC"])


class ResendTests(Base):
    def test_sending_again_asks_first_then_replaces_the_earlier_referral(self):
        first = self.refer().json()["id"]
        warn = self.refer()
        self.assertEqual(warn.status_code, 409)
        self.assertEqual(warn.json()["code"], "duplicate_warning")
        again = self.refer(confirm_duplicate=True)
        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual(Referral.objects.get(pk=first).approval_status, "disqualified")
        self.assertEqual(again.json()["approval_status"], "pending")

    def test_if_the_first_was_already_approved_the_new_one_is_disqualified(self):
        first = Referral.objects.get(pk=self.refer().json()["id"])
        first.approval_status = "approved"
        first.save()
        again = self.refer(confirm_duplicate=True)
        self.assertEqual(again.json()["approval_status"], "disqualified")
        self.assertEqual(Referral.objects.get(pk=first.pk).approval_status, "approved")

    def test_a_different_department_is_not_a_repeat(self):
        self.refer()
        self.assertEqual(self.refer(to_department="Surgery").status_code, 201)


class ApprovalAndPrintTests(Base):
    def test_outside_referrals_need_no_approval_but_wait_for_payments(self):
        BillableItem.objects.create(encounter=self.enc, description="Consultation", department="OPD", unit_price=300)
        rid = self.refer("external").json()["id"]
        self.assertEqual(self.reception.post(f"/api/clinic/referrals/{rid}/reception-approve/").status_code, 400)
        blocked = self.reception.post(f"/api/clinic/referrals/{rid}/print/")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("payment", str(blocked.json()).lower())
        self.pay_all(self.enc)
        done = self.reception.post(f"/api/clinic/referrals/{rid}/print/")
        self.assertEqual(done.status_code, 200, done.content)

    def test_branch_to_branch_needs_reception_approval_and_payments_before_print(self):
        rid = self.refer().json()["id"]
        self.pay_all(self.enc)
        self.assertEqual(self.reception.post(f"/api/clinic/referrals/{rid}/print/").status_code, 400)
        self.assertEqual(self.reception.post(f"/api/clinic/referrals/{rid}/reception-approve/").status_code, 200)
        self.assertEqual(self.reception.post(f"/api/clinic/referrals/{rid}/print/").status_code, 200)

    def test_printing_checks_the_patient_out(self):
        rid = self.refer("external").json()["id"]
        self.pay_all(self.enc)
        res = self.reception.post(f"/api/clinic/referrals/{rid}/print/").json()
        self.enc.refresh_from_db()
        self.assertEqual(self.enc.status, "closed")
        self.assertEqual(res["checkout"]["status"], "closed")
        self.assertIsNotNone(Referral.objects.get(pk=rid).printed_at)
        # printing again does not fail and does not reopen anything
        self.assertEqual(self.reception.post(f"/api/clinic/referrals/{rid}/print/").status_code, 200)

    def test_a_disqualified_referral_cannot_be_printed(self):
        first = self.refer().json()["id"]
        self.refer(confirm_duplicate=True)
        self.pay_all(self.enc)
        res = self.reception.post(f"/api/clinic/referrals/{first}/print/")
        self.assertEqual(res.status_code, 400)

    def test_only_reception_prints(self):
        rid = self.refer("external").json()["id"]
        self.assertEqual(self.doctor.post(f"/api/clinic/referrals/{rid}/print/").status_code, 403)


class ReceivingBranchTests(Base):
    def sent(self, **over):
        rid = self.refer(**over).json()["id"]
        self.reception.post(f"/api/clinic/referrals/{rid}/reception-approve/")
        return rid

    def test_the_receiving_manager_sees_it_only_after_reception_releases_it(self):
        rid = self.refer().json()["id"]
        self.assertEqual(self.east_manager.get("/api/clinic/referrals/incoming/").json(), [])
        self.reception.post(f"/api/clinic/referrals/{rid}/reception-approve/")
        rows = self.east_manager.get("/api/clinic/referrals/incoming/").json()
        self.assertEqual([r["id"] for r in rows], [rid])
        self.assertEqual(rows[0]["handover"]["phone"], "0911000111")
        self.assertEqual(self.manager.get("/api/clinic/referrals/incoming/").json(), [])  # the sender does not see it

    def test_approving_makes_the_patient_local_with_a_visit_and_no_consultation_charge(self):
        BillableService.objects.create(
            clinic_tin=TIN, branch_name="East", code="CON", name="Consultation", department="Consultation", unit_price=300,
            auto_add_on_registration=True,
        )
        rid = self.sent()
        res = self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        local = Patient.objects.get(branch_name="East", full_name="Hana Bekele")
        visit = Encounter.objects.get(patient=local)
        self.assertEqual((visit.priority, visit.referral_from_id), ("high", rid))
        self.assertEqual(visit.billables.count(), 0)
        self.assertIn("Suspected angina", visit.chart.clinical_notes)
        self.assertEqual(Referral.objects.get(pk=rid).approval_status, "approved")

    def test_consultation_is_charged_when_the_service_includes_referral_patients(self):
        BillableService.objects.create(
            clinic_tin=TIN, branch_name="East", code="CON", name="Consultation", department="Consultation", unit_price=300,
            auto_add_on_registration=True, includes_referral_patients=True,
        )
        rid = self.sent()
        self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        self.assertEqual(Encounter.objects.get(branch_name="East").billables.count(), 1)

    def test_a_referred_patient_with_an_open_visit_cannot_be_approved(self):
        rid = self.sent()
        local = Patient.objects.create(clinic_tin=TIN, branch_name="East", mrn="E-1", full_name="Hana Bekele", age=34, gender="Female", phone="0911000111")
        Encounter.objects.create(clinic_tin=TIN, branch_name="East", number="EE1", patient=local, arrival_type="new")
        res = self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "open_encounter")
        self.assertEqual(Referral.objects.get(pk=rid).approval_status, "pending")

    def test_an_existing_local_patient_is_reused(self):
        rid = self.sent()
        local = Patient.objects.create(clinic_tin=TIN, branch_name="East", mrn="E-1", full_name="Hana Bekele", age=34, gender="Female", phone="0911000111")
        self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        self.assertEqual(Patient.objects.filter(branch_name="East").count(), 1)
        self.assertEqual(Encounter.objects.get(branch_name="East").patient_id, local.id)

    def test_only_the_receiving_branchs_manager_decides(self):
        rid = self.sent()
        self.assertEqual(self.manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json").status_code, 404)
        self.assertEqual(self.east_doctor.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json").status_code, 403)

    def test_reject_and_a_disqualified_one_cannot_be_approved(self):
        rid = self.sent()
        self.assertEqual(self.east_manager.post(f"/api/clinic/referrals/{rid}/reject/").json()["approval_status"], "rejected")
        self.assertEqual(self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json").status_code, 400)

    def test_referral_patients_do_not_pay_for_services_that_do_not_include_them(self):
        rid = self.sent()
        self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        visit = Encounter.objects.get(branch_name="East")
        free = BillableService.objects.create(clinic_tin=TIN, branch_name="East", code="L1", name="CBC", department="Lab", unit_price=100, service_type="lab")
        paid = BillableService.objects.create(clinic_tin=TIN, branch_name="East", code="L2", name="ECG", department="Lab", unit_price=200, service_type="lab", includes_referral_patients=True)
        for svc in (free, paid):
            res = self.east_doctor.post("/api/clinic/orders/", {"encounter": visit.id, "order_type": "lab", "details": svc.name, "service": svc.id}, format="json")
            self.assertEqual(res.status_code, 201, res.content)
        by_name = {b.description.split(" (")[0]: b for b in visit.billables.all()}
        self.assertEqual((by_name["CBC"].unit_price, by_name["CBC"].payment_status), (0, "PaymentApproved"))
        self.assertEqual((by_name["ECG"].unit_price, by_name["ECG"].payment_status), (200, "AwaitingPayment"))

    def test_reports_mark_referral_patients_with_where_from_and_to(self):
        rid = self.sent()
        self.east_manager.post(f"/api/clinic/referrals/{rid}/approve/", {}, format="json")
        rows = self.east_manager.get("/api/clinic/reports/").json()["referral_patients"]
        self.assertEqual((rows[0]["patient"], rows[0]["referred_from"], rows[0]["referred_to"]), ("Hana Bekele", "Main", "East"))
        self.assertEqual(self.manager.get("/api/clinic/reports/").json()["referred_out"], 1)


class MedicineExpiryTests(Base):
    def medicine(self, days_to_expiry):
        supplier = Supplier.objects.create(clinic_tin=TIN, branch_name="Main", supplier_name="S")
        return Medicine.objects.create(
            clinic_tin=TIN, branch_name="Main", supplier=supplier, name="Amoxicillin", unit_price=10, cost=6, on_hand=50,
            min_threshold=0, batch_number="B1", received_quantity=50,
            expiry_date=(timezone.localdate() + timedelta(days=days_to_expiry)),
        )

    def rx(self, med, **over):
        body = {"encounter": self.enc.id, "order_type": "prescription", "details": "Twice a day", "medicine": med.id, "quantity": 1}
        body.update(over)
        return self.doctor.post("/api/clinic/orders/", body, format="json")

    def end(self, days):
        return (timezone.localdate() + timedelta(days=days)).isoformat()

    def test_an_expired_medicine_cannot_be_ordered(self):
        res = self.rx(self.medicine(-1))
        self.assertEqual(res.status_code, 400)
        self.assertIn("expired", str(res.json()).lower())

    def test_it_must_expire_at_least_five_days_after_the_treatment_ends(self):
        med = self.medicine(20)
        self.assertEqual(self.rx(med, duration_end=self.end(18)).status_code, 400)  # 2 days margin
        self.assertEqual(self.rx(med, duration_end=self.end(20)).status_code, 400)  # 0 days margin
        self.assertEqual(self.rx(med, duration_end=self.end(15)).status_code, 201)  # 5 days margin

    def test_between_five_and_ten_days_a_warning_is_kept_for_the_pharmacist(self):
        med = self.medicine(20)
        res = self.rx(med, duration_end=self.end(12))
        self.assertEqual(res.status_code, 201, res.content)
        self.assertIn("Warn the patient", res.json()["expiry_warning"])
        self.assertEqual(ClinicalOrder.objects.get(pk=res.json()["id"]).duration_end.isoformat(), self.end(12))

    def test_more_than_ten_days_of_margin_has_no_warning(self):
        res = self.rx(self.medicine(40), duration_end=self.end(10))
        self.assertEqual(res.json()["expiry_warning"], "")

    def test_till_medicine_ends_has_no_end_date_and_warns_when_it_expires_soon(self):
        res = self.rx(self.medicine(8), till_medicine_ends=True)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(res.json()["till_medicine_ends"])
        self.assertIn("expires", res.json()["expiry_warning"])

    def test_a_past_end_date_is_refused(self):
        self.assertEqual(self.rx(self.medicine(40), duration_end=self.end(-2)).status_code, 400)

    def test_a_medicine_without_an_expiry_date_is_unaffected(self):
        med = self.medicine(40)
        Medicine.objects.filter(pk=med.pk).update(expiry_date=None)
        self.assertEqual(self.rx(med, duration_end=self.end(3)).status_code, 201)


class StatusTreatmentTests(Base):
    FIELDS = [
        {"name": "Temperature", "result_type": "numeric", "unit": "C", "ref_low": "36.1", "ref_high": "37.5", "critical_high": "40"},
        {"name": "Pain", "result_type": "choice", "choices": "None,Mild,Severe"},
        {"name": "Remark", "result_type": "text"},
    ]

    def assign(self, enc=None, **over):
        body = {"encounter": (enc or self.enc).id, "stage": "observation", "fields": self.FIELDS}
        body.update(over)
        return self.doctor.post("/api/clinic/treatment-assignments/", body, format="json")

    def other_visit(self):
        p = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="M2", full_name="Kebede Alemu", age=50)
        return Encounter.objects.create(clinic_tin=TIN, branch_name="Main", number="E2", patient=p, arrival_type="new")

    def test_the_doctor_assigns_measurements_for_a_stage(self):
        res = self.assign()
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual((body["stage"], body["stage_label"], body["status"]), ("observation", "Under observation", "active"))
        self.assertEqual([f["name"] for f in body["fields"]], ["Temperature", "Pain", "Remark"])
        self.assertEqual(body["fields"][0]["reference_text"], "36.1 – 37.5")

    def test_only_open_visits_known_stages_and_good_fields_are_accepted(self):
        closed = self.other_visit()
        closed.status = "closed"
        closed.save()
        self.assertEqual(self.assign(closed).status_code, 400)
        self.assertEqual(self.assign(stage="whenever").status_code, 400)
        self.assertEqual(self.assign(fields=[]).status_code, 400)
        self.assertEqual(self.assign(fields=[{"name": "Pain", "result_type": "choice", "choices": "OnlyOne"}]).status_code, 400)
        self.assertEqual(self.assign(fields=[{"name": "", "result_type": "text"}]).status_code, 400)
        self.assertEqual(self.assign(fields=[{"name": "A", "result_type": "text"}, {"name": "a", "result_type": "text"}]).status_code, 400)
        self.assertEqual(self.nurse.post("/api/clinic/treatment-assignments/", {"encounter": self.enc.id, "stage": "initial", "fields": self.FIELDS}, format="json").status_code, 403)

    def test_the_nurse_fills_the_results_and_the_doctor_sees_them_flagged(self):
        a = self.assign().json()
        ids = [f["id"] for f in a["fields"]]
        res = self.nurse.post(f"/api/clinic/treatment-assignments/{a['id']}/results/", {"values": {ids[0]: "38.2", ids[1]: "Mild", ids[2]: ""}}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual((body["fields"][0]["value"], body["fields"][0]["flag"]), ("38.2", "high"))
        self.assertEqual((body["filled_count"], body["results_by_name"]), (2, "b6nurse"))
        seen = self.doctor.get(f"/api/clinic/treatment-assignments/?encounter={self.enc.id}").json()
        self.assertEqual(seen[0]["fields"][0]["value"], "38.2")

    def test_bad_readings_are_refused(self):
        a = self.assign().json()
        ids = [f["id"] for f in a["fields"]]
        url = f"/api/clinic/treatment-assignments/{a['id']}/results/"
        self.assertEqual(self.nurse.post(url, {"values": {ids[0]: "hot"}}, format="json").status_code, 400)
        self.assertEqual(self.nurse.post(url, {"values": {ids[1]: "Medium"}}, format="json").status_code, 400)
        self.assertEqual(self.nurse.post(url, {"values": {ids[0]: ""}}, format="json").status_code, 400)
        self.assertEqual(self.doctor.post(url, {"values": {ids[0]: "37"}}, format="json").status_code, 403)

    def test_reassign_copies_the_fields_and_stage_but_not_the_results(self):
        a = self.assign().json()
        self.nurse.post(f"/api/clinic/treatment-assignments/{a['id']}/results/", {"values": {a["fields"][0]["id"]: "37"}}, format="json")
        other = self.other_visit()
        res = self.doctor.post(f"/api/clinic/treatment-assignments/{a['id']}/reassign/", {"encounter": other.id}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        copy = res.json()
        self.assertEqual((copy["stage"], copy["patient_name"], copy["reassigned_from"]), ("observation", "Kebede Alemu", a["id"]))
        self.assertEqual([f["value"] for f in copy["fields"]], ["", "", ""])
        self.assertEqual([f["name"] for f in copy["fields"]], ["Temperature", "Pain", "Remark"])
        self.assertEqual(TreatmentAssignment.objects.count(), 2)

    def test_reassign_needs_a_different_open_patient(self):
        a = self.assign().json()
        url = f"/api/clinic/treatment-assignments/{a['id']}/reassign/"
        self.assertEqual(self.doctor.post(url, {"encounter": self.enc.id}, format="json").status_code, 400)
        gone = self.other_visit()
        gone.status = "closed"
        gone.save()
        self.assertEqual(self.doctor.post(url, {"encounter": gone.id}, format="json").status_code, 400)

    def test_checkout_expires_the_assignment_and_it_can_no_longer_be_filled(self):
        a = self.assign().json()
        self.pay_all(self.enc)
        self.assertEqual(self.reception.post(f"/api/clinic/encounters/{self.enc.id}/checkout/").status_code, 200)
        self.assertEqual(TreatmentAssignment.objects.get(pk=a["id"]).status, "expired")
        res = self.nurse.post(f"/api/clinic/treatment-assignments/{a['id']}/results/", {"values": {a["fields"][0]["id"]: "37"}}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("expired", str(res.json()))


class SavedSetTests(Base):
    """The doctor can write measurements fresh each time, or save a set and reuse it."""

    FIELDS = StatusTreatmentTests.FIELDS

    def save_set(self, name="Post-op check", **over):
        body = {"name": name, "fields": self.FIELDS}
        body.update(over)
        return self.doctor.post("/api/clinic/treatment-templates/", body, format="json")

    def test_a_set_is_saved_listed_and_reused_for_an_assignment(self):
        res = self.save_set()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.json()["name"], res.json()["field_count"], res.json()["created_by_name"]), ("Post-op check", 3, "b6doc"))
        listed = self.nurse.get("/api/clinic/treatment-templates/").json()
        self.assertEqual([t["name"] for t in listed], ["Post-op check"])
        saved_fields = listed[0]["fields"]
        again = self.doctor.post("/api/clinic/treatment-assignments/", {"encounter": self.enc.id, "stage": "post_procedure", "fields": saved_fields}, format="json")
        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual([f["name"] for f in again.json()["fields"]], ["Temperature", "Pain", "Remark"])
        self.assertEqual(again.json()["fields"][0]["reference_text"], "36.1 – 37.5")

    def test_names_must_be_unique_and_fields_valid(self):
        self.save_set()
        self.assertEqual(self.save_set(name="  post-op CHECK ").status_code, 400)
        self.assertEqual(self.save_set(name="").status_code, 400)
        self.assertEqual(self.save_set(name="Other", fields=[]).status_code, 400)
        self.assertEqual(self.save_set(name="Bad", fields=[{"name": "Pain", "result_type": "choice", "choices": "One"}]).status_code, 400)

    def test_only_the_doctor_saves_and_deletes_and_a_deleted_name_can_be_used_again(self):
        self.assertEqual(self.nurse.post("/api/clinic/treatment-templates/", {"name": "X", "fields": self.FIELDS}, format="json").status_code, 403)
        tid = self.save_set().json()["id"]
        self.assertEqual(self.nurse.delete(f"/api/clinic/treatment-templates/{tid}/").status_code, 403)
        self.assertEqual(self.doctor.delete(f"/api/clinic/treatment-templates/{tid}/").status_code, 204)
        self.assertEqual(self.doctor.get("/api/clinic/treatment-templates/").json(), [])
        self.assertEqual(self.save_set().status_code, 201)

    def test_sets_are_per_branch(self):
        self.save_set()
        self.assertEqual(self.east_doctor.get("/api/clinic/treatment-templates/").json(), [])
