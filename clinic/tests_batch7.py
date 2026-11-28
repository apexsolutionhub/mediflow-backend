"""Batch 7: patient history table, audit log screen, early sync warning, and the reporting pass."""
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import AuditEvent, UserProfile
from clinic import sync_views
from clinic.models import (
    Appointment,
    Encounter,
    Patient,
    PaymentTransaction,
    RefundTransaction,
    SyncState,
    TreatmentAssignment,
)

TIN = "B7-TIN"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


def make_user(name, role, branch="Main"):
    user = get_user_model().objects.create_user(name, password="pw-123456")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name=branch)
    client = APIClient()
    client.force_authenticate(user)
    return client, user


class Base(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.manager, self.manager_user = make_user("b7mgr", "manager")
        self.reception, _ = make_user("b7rec", "reception")
        self.n = 0

    def patient(self, name, **kw):
        self.n += 1
        return Patient.objects.create(
            clinic_tin=TIN, branch_name="Main", mrn=f"M{self.n}", full_name=name, age=30, phone=f"09{self.n:08d}", **kw
        )

    def visit(self, patient, paid=0, method="cash", closed=False, number=None):
        self.n += 1
        enc = Encounter.objects.create(
            clinic_tin=TIN, branch_name="Main", number=number or f"E{self.n}", patient=patient, arrival_type="new",
            status="closed" if closed else "open", closed_at=timezone.now() if closed else None,
        )
        if paid:
            PaymentTransaction.objects.create(encounter=enc, receipt_number=f"R{self.n}", amount=paid, tender_method=method)
        return enc


class PatientHistoryTests(Base):
    def test_only_the_manager_sees_it(self):
        self.assertEqual(self.reception.get("/api/clinic/patient-history/").status_code, 403)
        self.assertEqual(self.reception.get("/api/clinic/patient-history/options/").status_code, 403)

    def test_all_shows_one_row_per_patient_with_a_visit_count_and_their_visits(self):
        a = self.patient("Abel Tesfaye")
        self.visit(a, 100, closed=True)
        self.visit(a, 50, method="bank", closed=True)
        self.patient("Zelalem Kassa")
        body = self.manager.get("/api/clinic/patient-history/").json()
        self.assertEqual((body["mode"], body["count"], body["page_size"]), ("patients", 2, 10))
        row = body["results"][0]
        self.assertEqual((row["patient_name"], row["visit_count"], Decimal(str(row["total_paid"]))), ("Abel Tesfaye", 2, Decimal("150")))
        self.assertEqual({v["methods"][0] for v in row["visits"]}, {"cash", "bank"})
        self.assertIsNotNone(row["visits"][0]["checked_out_at"])
        self.assertEqual(body["results"][1]["visit_count"], 0)

    def test_pages_have_ten_rows(self):
        for i in range(23):
            self.patient(f"Patient {i:02d}")
        first = self.manager.get("/api/clinic/patient-history/").json()
        third = self.manager.get("/api/clinic/patient-history/?page=3").json()
        self.assertEqual((first["count"], len(first["results"]), len(third["results"])), (23, 10, 3))

    def test_search_matches_name_mrn_and_phone(self):
        a = self.patient("Hana Bekele")
        self.patient("Kebede Alemu")
        for q in ("hana", a.mrn, a.phone[-6:]):
            rows = self.manager.get("/api/clinic/patient-history/", {"q": q}).json()["results"]
            self.assertEqual([r["patient_name"] for r in rows], ["Hana Bekele"], q)

    def test_one_patient_shows_their_visits_as_plain_rows_ten_a_page(self):
        a = self.patient("Abel Tesfaye")
        for _ in range(12):
            self.visit(a, 10, closed=True)
        other = self.patient("Other")
        self.visit(other, 999, closed=True)
        body = self.manager.get("/api/clinic/patient-history/", {"patient": a.id}).json()
        self.assertEqual((body["mode"], body["count"], len(body["results"])), ("visits", 12, 10))
        self.assertTrue(all(r["patient_name"] == "Abel Tesfaye" and r["mrn"] == a.mrn for r in body["results"]))
        second = self.manager.get("/api/clinic/patient-history/", {"patient": a.id, "page": 2}).json()
        self.assertEqual(len(second["results"]), 2)

    def test_the_filter_lists_everyone_and_a_total_includes_refund_info(self):
        a = self.patient("Abel Tesfaye")
        enc = self.visit(a, 100, closed=True)
        RefundTransaction.objects.create(encounter=enc, amount=30, reason="x")
        self.patient("Beza")
        opts = self.manager.get("/api/clinic/patient-history/options/").json()
        self.assertEqual([o["name"] for o in opts], ["Abel Tesfaye", "Beza"])
        visit = self.manager.get("/api/clinic/patient-history/").json()["results"][0]["visits"][0]
        self.assertEqual((Decimal(str(visit["total_paid"])), Decimal(str(visit["refunded"]))), (Decimal("100"), Decimal("30")))

    def test_another_branchs_patients_are_not_listed(self):
        Patient.objects.create(clinic_tin=TIN, branch_name="East", mrn="E1", full_name="East Person", age=40)
        self.patient("Main Person")
        names = [r["patient_name"] for r in self.manager.get("/api/clinic/patient-history/").json()["results"]]
        self.assertEqual(names, ["Main Person"])

    def test_the_detail_has_chart_orders_follow_ups_and_payments_with_dates(self):
        a = self.patient("Abel Tesfaye", allergies="Penicillin")
        enc = self.visit(a, 300, closed=True)
        from clinic.models import ClinicalOrder, DoctorChart

        DoctorChart.objects.create(encounter=enc, chief_complaint="Cough", diagnosis="Bronchitis")
        ClinicalOrder.objects.create(encounter=enc, order_type="lab", status="Completed", details="CBC", result_text="Normal")
        ClinicalOrder.objects.create(encounter=enc, order_type="prescription", status="Dispensed", details="Amoxicillin")
        Appointment.objects.create(clinic_tin=TIN, branch_name="Main", patient=a, scheduled_at=timezone.now() + timedelta(days=3), kind="follow_up")
        body = self.manager.get(f"/api/clinic/patient-history/{a.id}/").json()
        self.assertEqual((body["patient"]["name"], body["patient"]["allergies"]), ("Abel Tesfaye", "Penicillin"))
        visit = body["visits"][0]
        self.assertEqual((visit["chart"]["diagnosis"], len(visit["lab_radiology"]), len(visit["pharmacy"])), ("Bronchitis", 1, 1))
        self.assertEqual(Decimal(str(visit["total_paid"])), Decimal("300"))
        self.assertTrue(visit["registered_at"] and visit["checked_out_at"])
        self.assertEqual(len(body["follow_ups"]), 1)
        self.assertEqual(self.manager.get("/api/clinic/patient-history/99999/").status_code, 404)


class AuditLogTests(Base):
    def event(self, name="Selam", role="reception", action="create", summary="Created patient #1", branch="Main", when=None):
        e = AuditEvent.objects.create(
            clinic_tin=TIN, branch_name=branch, actor_name=name, actor_username="u", actor_role=role, action=action,
            entity="patient", entity_id="1", summary=summary, details={"path": "/api/clinic/patients/", "fields": {"full_name": "X"}},
        )
        if when:
            AuditEvent.objects.filter(pk=e.pk).update(created_at=when)
        return e

    def test_only_the_manager_reads_it(self):
        self.assertEqual(self.reception.get("/api/user/audit/").status_code, 403)

    def test_ten_rows_a_page_newest_first_with_filter_choices(self):
        for i in range(14):
            self.event(summary=f"Event {i}")
        body = self.manager.get("/api/user/audit/").json()
        self.assertEqual((body["count"], len(body["results"]), body["results"][0]["summary"]), (14, 10, "Event 13"))
        self.assertEqual(len(self.manager.get("/api/user/audit/?page=2").json()["results"]), 4)
        self.assertEqual((body["roles"], body["actions"]), (["reception"], ["create"]))

    def test_search_role_action_and_date_filters(self):
        self.event(name="Selam", role="reception", summary="Created patient")
        self.event(name="Dr Abel", role="doctor", action="update", summary="Changed chart")
        old = self.event(name="Old", role="nurse", summary="Old thing", when=timezone.now() - timedelta(days=30))
        get = lambda **q: [r["actor_name"] for r in self.manager.get("/api/user/audit/", q).json()["results"]]
        self.assertEqual(get(q="chart"), ["Dr Abel"])
        self.assertEqual(get(role="doctor"), ["Dr Abel"])
        self.assertIn("Selam", get(action="create"))
        recent = (timezone.now() - timedelta(days=2)).date().isoformat()
        self.assertNotIn("Old", get(date_from=recent))
        self.assertEqual(get(date_to=(timezone.now() - timedelta(days=20)).date().isoformat()), ["Old"])
        self.assertIsNotNone(old)

    def test_detail_shows_the_recorded_fields_and_other_branches_are_hidden(self):
        mine = self.event()
        theirs = self.event(branch="East")
        detail = self.manager.get(f"/api/user/audit/{mine.id}/").json()
        self.assertEqual(detail["details"]["fields"]["full_name"], "X")
        self.assertEqual(self.manager.get(f"/api/user/audit/{theirs.id}/").status_code, 404)
        self.assertEqual(self.manager.get("/api/user/audit/").json()["count"], 1)

    def test_a_real_change_shows_up_with_who_did_it(self):
        self.reception.post("/api/clinic/patients/", {"full_name": "Newly Registered", "age": 20, "gender": "Male", "phone": "0912345678"}, format="json")
        rows = self.manager.get("/api/user/audit/", {"q": "patient"}).json()["results"]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["actor_role"], "reception")


class SyncEarlyWarningTests(Base):
    def run_check(self, last_days_ago, allowed=7, seeded=True):
        tenant = SimpleNamespace(ops_mode="offline", clinic_tin=TIN)
        now = timezone.now()
        last = now - timedelta(days=last_days_ago)
        state = SyncState.objects.create(
            clinic_tin=TIN, branch_name="Main", seeded_at=now - timedelta(days=30) if seeded else None,
            last_successful_sync_at=last, last_sync_token=last.isoformat() if seeded else "",
        )
        scope = SimpleNamespace(tin=TIN, branch="Main")
        with mock.patch.object(sync_views, "_is_node", return_value=True), mock.patch("clinic.sync_gate.allowed_days", return_value=allowed):
            return sync_views._idle_check(scope, state, tenant)

    def test_nothing_to_push_and_not_due_gives_a_warning_only(self):
        out = self.run_check(last_days_ago=1)
        self.assertEqual((out["pending_push"], out["nothing_to_push"], out["warn_only"]), (0, True, True))
        self.assertEqual((out["due_after_days"], out["next_due_in_days"]), (7, 6))

    def test_two_days_when_the_payment_period_is_short(self):
        out = self.run_check(last_days_ago=1, allowed=2)
        self.assertEqual((out["due_after_days"], out["next_due_in_days"], out["warn_only"]), (2, 1, True))

    def test_when_the_due_date_has_arrived_there_is_no_warning(self):
        out = self.run_check(last_days_ago=7)
        self.assertEqual((out["due_reached"], out["warn_only"]), (True, False))

    def test_changed_rows_mean_there_is_something_to_push(self):
        state_now = timezone.now()
        SyncState.objects.create(clinic_tin=TIN, branch_name="Main", seeded_at=state_now - timedelta(days=3),
                                 last_successful_sync_at=state_now - timedelta(days=1), last_sync_token=(state_now - timedelta(days=1)).isoformat())
        self.patient("New since the last sync")
        state = SyncState.objects.get(clinic_tin=TIN, branch_name="Main")
        scope = SimpleNamespace(tin=TIN, branch="Main")
        tenant = SimpleNamespace(ops_mode="offline", clinic_tin=TIN)
        with mock.patch.object(sync_views, "_is_node", return_value=True), mock.patch("clinic.sync_gate.allowed_days", return_value=7):
            out = sync_views._idle_check(scope, state, tenant)
        self.assertGreater(out["pending_push"], 0)
        self.assertFalse(out["warn_only"])

    def test_never_synced_or_not_seeded_is_not_idle(self):
        out = self.run_check(last_days_ago=1, seeded=False)
        self.assertIsNone(out["pending_push"])
        self.assertFalse(out["warn_only"])

    def test_online_clinics_and_the_cloud_get_nothing(self):
        scope = SimpleNamespace(tin=TIN, branch="Main")
        with mock.patch.object(sync_views, "_is_node", return_value=False):
            self.assertFalse(sync_views._idle_check(scope, None, SimpleNamespace(ops_mode="offline"))["applies"])
        with mock.patch.object(sync_views, "_is_node", return_value=True):
            self.assertFalse(sync_views._idle_check(scope, None, SimpleNamespace(ops_mode="online"))["applies"])


class ReportingPassTests(Base):
    def test_reports_carry_priority_treatment_and_expiry_warning_counts(self):
        a, b = self.patient("A One"), self.patient("B Two")
        high = self.visit(a)
        high.priority = "very_high"
        high.save()
        self.visit(b)
        TreatmentAssignment.objects.create(encounter=high, stage="initial", status="expired", expired_at=timezone.now())
        from clinic.models import ClinicalOrder

        ClinicalOrder.objects.create(encounter=high, order_type="prescription", status="PaymentApproved", details="x", expiry_warning="Warn")
        data = self.manager.get("/api/clinic/reports/").json()
        counts = {r["priority"]: r["count"] for r in data["priority_breakdown"]}
        self.assertEqual((counts["very_high"], counts["high"], counts["normal"]), (1, 0, 1))
        self.assertEqual((data["treatment"]["assigned"], data["treatment"]["expired_unfilled"], data["expiry_warnings"]), (1, 1, 1))
