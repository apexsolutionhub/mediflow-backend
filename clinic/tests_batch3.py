"""Batch 3: vitals by assessment type and stage, and the nurse edit rule."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic import vitals
from clinic.models import Encounter, NurseNote, Patient

TIN = "B3-TIN"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


class VitalsRulesTests(SimpleTestCase):
    def keys(self, assessment, stage):
        return vitals.vitals_for(assessment, stage)

    def test_initial_asks_the_full_set_for_the_type(self):
        self.assertEqual(self.keys("general_adult", "initial"), ["bp", "pulse", "resp_rate", "temp", "spo2", "weight", "height", "bmi", "pain"])
        self.assertIn("muac", self.keys("child", "initial"))
        self.assertIn("head_circ", self.keys("newborn", "initial"))
        self.assertIn("fundal_height", self.keys("antenatal", "initial"))
        self.assertEqual(self.keys("chronic", "initial"), ["bp", "weight", "bmi", "glucose"])

    def test_observation_asks_pulse_bp_and_spo2_for_every_type(self):
        for assessment in vitals.ASSESSMENTS:
            self.assertEqual(self.keys(assessment, "observation"), ["pulse", "bp", "spo2"])

    def test_before_discharge_asks_the_key_vitals_and_a_note(self):
        self.assertEqual(self.keys("general_adult", "before_discharge"), ["bp", "pulse", "temp", "spo2"])
        self.assertEqual(self.keys("newborn", "before_discharge"), ["temp", "pulse"])
        self.assertTrue(vitals.config()["assessments"][0]["stages"]["before_discharge"]["note_required"])

    def test_bmi_is_worked_out_from_weight_and_height(self):
        out = vitals.clean("general_adult", "initial", {"weight": "68", "height": "170"})
        self.assertEqual(out["bmi"], "23.5")

    def test_bad_readings_are_refused(self):
        for assessment, stage, raw in [
            ("general_adult", "initial", {"bp": "80/120"}),
            ("general_adult", "initial", {"bp": "abc"}),
            ("general_adult", "initial", {"temp": "98.6"}),
            ("general_adult", "initial", {"head_circ": "35"}),  # not asked for an adult
            ("general_adult", "observation", {"weight": "70"}),  # not asked under observation
            ("antenatal", "initial", {"urine_protein": "maybe"}),
            ("nope", "initial", {}),
            ("general_adult", "nope", {}),
        ]:
            with self.assertRaises(vitals.VitalsError, msg=str((assessment, stage, raw))):
                vitals.clean(assessment, stage, raw)

    def test_display_uses_the_type_specific_label(self):
        rows = vitals.display("newborn", {"pulse": "140", "temp": "36.8"})
        self.assertEqual([(r["key"], r["label"]) for r in rows], [("pulse", "Heart rate"), ("temp", "Temperature")])


class NurseNoteApiTests(TestCase):
    def setUp(self):
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        user = get_user_model().objects.create_user("b3nurse", password="pw-123456")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role="nurse", branch_name="Main")
        self.api = APIClient()
        self.api.force_authenticate(user)
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="M1", full_name="Hana Bekele", age=30)
        self.enc = Encounter.objects.create(clinic_tin=TIN, branch_name="Main", number="E1", patient=patient, arrival_type="new")

    def save(self, vitals_=None, **over):
        body = {"encounter": self.enc.id, "assessment_type": "general_adult", "stage": "initial", "content": "", "vitals": {"pulse": "78"} if vitals_ is None else vitals_}
        body.update(over)
        return self.api.post("/api/clinic/nurse-notes/", body, format="json")

    def test_the_screen_is_built_from_the_config_endpoint(self):
        data = self.api.get("/api/clinic/nurse-notes/vitals-config/").json()
        self.assertEqual([a["value"] for a in data["assessments"]], ["general_adult", "child", "newborn", "antenatal", "chronic"])
        self.assertEqual([s["value"] for s in data["stages"]][:2], ["initial", "reassessment"])
        self.assertEqual(data["default_stage"], "initial")

    def test_a_note_keeps_its_type_stage_and_readable_readings(self):
        res = self.save({"weight": "68", "height": "170", "bp": "120/80"})
        self.assertEqual(res.status_code, 201, res.content)
        body = res.json()
        self.assertEqual((body["assessment_label"], body["stage_label"]), ("General adult", "Initial"))
        self.assertEqual(body["vitals"]["bmi"], "23.5")
        self.assertIn(("Blood pressure", "mmHg", "120/80"), [(r["label"], r["unit"], r["value"]) for r in body["vitals_display"]])

    def test_a_second_save_for_the_same_type_and_stage_edits_the_first(self):
        self.assertEqual(self.save({"pulse": "78"}).status_code, 201)
        res = self.save({"pulse": "82", "temp": "37.1"})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["edited_existing"])
        self.assertEqual(NurseNote.objects.count(), 1)
        self.assertEqual(NurseNote.objects.get().vitals, {"pulse": "82", "temp": "37.1"})

    def test_another_stage_or_type_is_a_separate_record(self):
        self.save({"pulse": "78"})
        self.assertEqual(self.save({"pulse": "80"}, stage="reassessment").status_code, 201)
        self.assertEqual(self.save({"weight": "9", "pulse": "120"}, assessment_type="child").status_code, 201)
        self.assertEqual(NurseNote.objects.count(), 3)

    def test_under_observation_keeps_every_timed_reading(self):
        for pulse in ("80", "84", "88"):
            self.assertEqual(self.save({"pulse": pulse}, stage="observation").status_code, 201)
        self.assertEqual(NurseNote.objects.filter(stage="observation").count(), 3)

    def test_another_visit_is_a_separate_record(self):
        self.save({"pulse": "78"})
        other = Encounter.objects.create(clinic_tin=TIN, branch_name="Main", number="E2", patient=self.enc.patient, arrival_type="returning")
        self.assertEqual(self.save({"pulse": "70"}, encounter=other.id).status_code, 201)

    def test_before_discharge_needs_a_note(self):
        res = self.save({"pulse": "74"}, stage="before_discharge")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.save({"pulse": "74"}, stage="before_discharge", content="Walking, pain free").status_code, 201)

    def test_an_empty_note_is_refused(self):
        self.assertEqual(self.save({}, content="").status_code, 400)

    def test_wrong_readings_are_refused_with_a_reason(self):
        res = self.save({"temp": "98.6"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Temperature", str(res.json()))

    def test_the_older_form_still_saves(self):
        res = self.api.post(
            "/api/clinic/nurse-notes/",
            {"encounter": self.enc.id, "note_type": "assessment", "content": "Settled", "vitals": {"bp": "120/80", "pulse": "78", "temp": "36.8", "spo2": "98"}},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual((res.json()["assessment_type"], res.json()["stage"]), ("general_adult", "initial"))
