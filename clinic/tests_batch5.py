"""Batch 5: appointment registration, statuses, rescheduling, overlaps, priority ordering and registration rules."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import Appointment, BillableService, ClinicalOrder, Encounter, Patient, PaymentRule

TIN = "B5-TIN"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def make_user(name, role):
    user = get_user_model().objects.create_user(name, password="pw-123456")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name="Main")
    client = APIClient()
    client.force_authenticate(user)
    return client


class Base(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.reception = make_user("b5rec", "reception")
        self.manager = make_user("b5mgr", "manager")
        self.doctor = make_user("b5doc", "doctor")
        self.tomorrow = (timezone.now() + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)

    def patient(self, name="Hana Bekele", mrn="M1"):
        return Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn=mrn, full_name=name, age=30, phone=mrn)

    def register(self, name="Abel Tesfaye", when=None, **over):
        body = {
            "full_name": name, "age": 41, "gender": "Male", "phone": "0911" + str(abs(hash(name)) % 10**6),
            "scheduled_at": (when or self.tomorrow).isoformat(), "reason": "Check-up",
        }
        body.update(over)
        return self.reception.post("/api/clinic/appointments/register/", body, format="json")

    def book(self, patient, when=None, **over):
        body = {"patient": patient.id, "scheduled_at": (when or self.tomorrow).isoformat(), "reason": "Visit"}
        body.update(over)
        return self.reception.post("/api/clinic/appointments/", body, format="json")


class RegistrationTests(Base):
    def test_appointment_registration_creates_the_patient_and_a_registry_appointment(self):
        res = self.register(priority="high")
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertEqual((data["kind"], data["status"], data["priority"]), ("registry", "scheduled", "high"))
        self.assertTrue(Patient.objects.filter(full_name="Abel Tesfaye").exists())

    def test_a_registered_patient_is_not_registered_again(self):
        self.register()
        again = self.register(when=self.tomorrow + timedelta(days=1))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["code"], "patient_exists")
        self.assertEqual(Patient.objects.count(), 1)

    def test_past_times_and_missing_times_are_refused(self):
        self.assertEqual(self.register(when=timezone.now() - timedelta(hours=2)).status_code, 400)
        self.assertEqual(self.register(scheduled_at="").status_code, 400)

    def test_priority_must_be_one_of_the_three(self):
        self.assertEqual(self.register(priority="urgent").status_code, 400)

    def test_rules_chosen_at_registration_are_kept_and_must_be_this_branchs_rule(self):
        rule = PaymentRule.objects.create(
            clinic_tin=TIN, branch_name="Main", kind="discount", name="Staff", reason="Staff", all_services=True, percent=10
        )
        ok = self.register(discount_rule=rule.id)
        self.assertEqual(ok.json()["discount_rule_name"], "Staff")
        wrong_kind = self.register(name="Selam T", refund_rule=rule.id)
        self.assertEqual(wrong_kind.status_code, 400)

    def test_only_reception_registers_appointments(self):
        res = self.doctor.post("/api/clinic/appointments/register/", {"full_name": "X"}, format="json")
        self.assertEqual(res.status_code, 403)


class NoSecondAppointmentTests(Base):
    def test_a_patient_with_an_upcoming_appointment_cannot_get_another(self):
        patient = self.patient()
        self.assertEqual(self.book(patient).status_code, 201)
        res = self.book(patient, when=self.tomorrow + timedelta(days=3))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "open_appointment")
        self.assertEqual(Appointment.objects.count(), 1)

    def test_after_the_first_one_is_done_or_cancelled_a_new_one_is_fine(self):
        patient = self.patient()
        first = self.book(patient).json()["id"]
        self.reception.post(f"/api/clinic/appointments/{first}/set-status/", {"status": "cancelled"}, format="json")
        self.assertEqual(self.book(patient, when=self.tomorrow + timedelta(days=2)).status_code, 201)

    def test_a_doctor_follow_up_in_the_same_visit_still_edits_the_existing_one(self):
        patient = self.patient()
        body = {"patient": patient.id, "kind": "follow_up", "encounter_number": "E1", "reason": "Follow-up"}
        self.doctor.post("/api/clinic/appointments/", {**body, "scheduled_at": self.tomorrow.isoformat()}, format="json")
        res = self.doctor.post("/api/clinic/appointments/", {**body, "scheduled_at": (self.tomorrow + timedelta(days=4)).isoformat()}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["edited_existing"])
        self.assertEqual(Appointment.objects.count(), 1)


class StatusAndRescheduleTests(Base):
    def setUp(self):
        super().setUp()
        self.patient_ = self.patient()
        self.appt = self.book(self.patient_).json()["id"]

    def set_status(self, status, pk=None):
        return self.reception.post(f"/api/clinic/appointments/{pk or self.appt}/set-status/", {"status": status}, format="json")

    def test_done_no_show_and_cancelled_are_kept_and_final(self):
        for status in ("done", "no_show", "cancelled"):
            other = self.patient(f"P {status}", f"M-{status}")
            pk = self.book(other, when=self.tomorrow + timedelta(hours=2)).json()["id"]
            res = self.set_status(status, pk)
            self.assertEqual(res.json()["status"], status)
            self.assertEqual(self.set_status("done", pk).status_code, 400)

    def test_a_bad_status_is_refused(self):
        self.assertEqual(self.set_status("lost").status_code, 400)

    def test_reschedule_moves_it_and_marks_it_rescheduled(self):
        new = self.tomorrow + timedelta(days=2, hours=3)
        res = self.reception.post(f"/api/clinic/appointments/{self.appt}/reschedule/", {"scheduled_at": new.isoformat()}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual((res.json()["status"], res.json()["rescheduled_count"]), ("rescheduled", 1))
        # a rescheduled appointment still counts as the patient's pending one
        self.assertEqual(self.book(self.patient_, when=new + timedelta(days=5)).status_code, 409)

    def test_reschedule_needs_a_new_future_time(self):
        url = f"/api/clinic/appointments/{self.appt}/reschedule/"
        self.assertEqual(self.reception.post(url, {"scheduled_at": self.tomorrow.isoformat()}, format="json").status_code, 400)
        past = (timezone.now() - timedelta(days=1)).isoformat()
        self.assertEqual(self.reception.post(url, {"scheduled_at": past}, format="json").status_code, 400)
        self.assertEqual(self.reception.post(url, {}, format="json").status_code, 400)

    def test_a_cancelled_appointment_cannot_be_rescheduled(self):
        self.set_status("cancelled")
        res = self.reception.post(f"/api/clinic/appointments/{self.appt}/reschedule/", {"scheduled_at": (self.tomorrow + timedelta(days=1)).isoformat()}, format="json")
        self.assertEqual(res.status_code, 400)


class OverlapTests(Base):
    def setUp(self):
        super().setUp()
        self.first = self.book(self.patient("Hana Bekele", "M1")).json()["id"]
        self.other = self.patient("Kebede Alemu", "M2")

    def test_the_exact_same_date_and_time_asks_what_to_do(self):
        res = self.book(self.other)
        self.assertEqual(res.status_code, 409)
        body = res.json()
        self.assertEqual(body["code"], "appointment_overlap")
        self.assertEqual(body["overlaps"][0]["patient_name"], "Hana Bekele")

    def test_the_same_day_at_another_time_is_not_an_overlap(self):
        self.assertEqual(self.book(self.other, when=self.tomorrow + timedelta(minutes=30)).status_code, 201)

    def test_merge_keeps_both_linked(self):
        res = self.book(self.other, overlap="merge")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["merged_with"], self.first)
        self.assertEqual(res.json()["merged_with_name"], "Hana Bekele")
        self.assertEqual(Appointment.objects.get(pk=self.first).status, "scheduled")

    def test_cancel_saved_cancels_the_other_one(self):
        res = self.book(self.other, overlap="cancel_saved")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Appointment.objects.get(pk=self.first).status, "cancelled")

    def test_a_cancelled_appointment_does_not_block_its_slot(self):
        self.reception.post(f"/api/clinic/appointments/{self.first}/set-status/", {"status": "cancelled"}, format="json")
        self.assertEqual(self.book(self.other).status_code, 201)

    def test_reschedule_onto_a_saved_time_asks_too(self):
        second = self.book(self.other, when=self.tomorrow + timedelta(hours=3)).json()["id"]
        url = f"/api/clinic/appointments/{second}/reschedule/"
        body = {"scheduled_at": self.tomorrow.isoformat()}
        self.assertEqual(self.reception.post(url, body, format="json").json()["code"], "appointment_overlap")
        self.assertEqual(self.reception.post(url, {**body, "overlap": "merge"}, format="json").status_code, 200)

    def test_registration_checks_overlap_before_saving_anything(self):
        res = self.register()
        self.assertEqual(res.status_code, 409)
        self.assertEqual(Patient.objects.filter(full_name="Abel Tesfaye").count(), 0)
        self.assertEqual(self.register(overlap="merge").status_code, 201)


class ListAndReportTests(Base):
    def test_tabs_filter_by_kind_and_range(self):
        a, b = self.patient("A One", "M1"), self.patient("B Two", "M2")
        self.book(a)
        self.doctor.post("/api/clinic/appointments/", {"patient": b.id, "scheduled_at": (self.tomorrow + timedelta(hours=2)).isoformat(), "kind": "follow_up", "encounter_number": "E1"}, format="json")
        reg = self.reception.get("/api/clinic/appointments/?kind=registry&range=upcoming").json()
        reg = reg["results"] if isinstance(reg, dict) else reg
        self.assertEqual([x["patient_name"] for x in reg], ["A One"])
        fol = self.reception.get("/api/clinic/appointments/?kind=follow_up").json()
        fol = fol["results"] if isinstance(fol, dict) else fol
        self.assertEqual([x["patient_name"] for x in fol], ["B Two"])

    def test_reports_count_appointments_by_status(self):
        today = timezone.now() + timedelta(hours=1)
        patients = [self.patient(f"P{i}", f"M{i}") for i in range(4)]
        ids = []
        for i, p in enumerate(patients):
            Appointment.objects.create(clinic_tin=TIN, branch_name="Main", patient=p, scheduled_at=today + timedelta(minutes=i * 5))
            ids.append(Appointment.objects.latest("id").id)
        for pk, status in zip(ids, ("done", "no_show", "cancelled")):
            self.reception.post(f"/api/clinic/appointments/{pk}/set-status/", {"status": status}, format="json")
        self.reception.post(f"/api/clinic/appointments/{ids[3]}/reschedule/", {"scheduled_at": (today + timedelta(hours=2)).isoformat()}, format="json")
        report = self.manager.get("/api/clinic/reports/").json()["appointments"]
        counts = {r["status"]: r["count"] for r in report["by_status"]}
        self.assertEqual((counts["done"], counts["no_show"], counts["cancelled"], counts["rescheduled"]), (1, 1, 1, 1))


class PriorityOrderTests(Base):
    def open_visit(self, name, mrn, priority):
        patient = self.patient(name, mrn)
        res = self.reception.post(
            "/api/clinic/encounters/", {"patient_id": patient.id, "arrival_type": "new", "priority": priority}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        return Encounter.objects.get(pk=res.json()["id"])

    def test_the_today_board_puts_very_high_first_then_high_then_normal_first_in_first_out(self):
        self.open_visit("Normal A", "M1", "normal")
        self.open_visit("Normal B", "M2", "normal")
        self.open_visit("High C", "M3", "high")
        self.open_visit("Urgent D", "M4", "very_high")
        rows = self.reception.get("/api/clinic/encounters/?today=1").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([r["patient"]["full_name"] for r in rows], ["Urgent D", "High C", "Normal A", "Normal B"])

    def test_the_lab_queue_follows_priority_even_when_the_request_came_last(self):
        low = self.open_visit("Normal A", "M1", "normal")
        urgent = self.open_visit("Urgent D", "M4", "very_high")
        for enc in (low, urgent):
            ClinicalOrder.objects.create(encounter=enc, order_type="lab", status="PaymentApproved", details="CBC")
        rows = self.reception.get("/api/clinic/orders/?queue=lab").json()
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertEqual([r["encounter"] for r in rows], [urgent.id, low.id])

    def test_discount_and_refund_rules_are_kept_on_the_visit(self):
        rule = PaymentRule.objects.create(clinic_tin=TIN, branch_name="Main", kind="refund", name="R", reason="r", all_services=True, percent=50)
        patient = self.patient()
        res = self.reception.post("/api/clinic/encounters/", {"patient_id": patient.id, "arrival_type": "new", "refund_rule": rule.id}, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["refund_rule_name"], "R")
        bad = self.reception.post("/api/clinic/encounters/", {"patient_id": self.patient("Z", "M9").id, "arrival_type": "new", "discount_rule": rule.id}, format="json")
        self.assertEqual(bad.status_code, 400)
