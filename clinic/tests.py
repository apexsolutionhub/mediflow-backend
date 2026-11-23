from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from api.models import UserProfile
from clinic.models import BillableService, ClinicalOrder, Encounter, Patient, LabTestParameter

TIN = "TEST-TIN-1"


@mock.patch("tenants.permissions.TenantBillingAccessPermission.has_permission", return_value=True)
class StructuredLabResultTests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.doctor = self._user("doc", "doctor")
        self.lab = self._user("labtech", "lab")
        self.manager = self._user("mgr", "manager")
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="M1", full_name="Test Patient")
        self.encounter = Encounter.objects.create(
            clinic_tin=TIN, branch_name="Main", number="E1", patient=patient, arrival_type="new"
        )
        self.service = BillableService.objects.create(
            clinic_tin=TIN, branch_name="Main", code="CBC", name="CBC", department="Laboratory",
            service_type="lab", unit_price=100, requires_payment_before_work=False,
        )
        self.hb = LabTestParameter.objects.create(
            service=self.service, name="Hemoglobin", unit="g/dL", ref_low=12, ref_high=17, critical_low=7
        )
        self.rdt = LabTestParameter.objects.create(
            service=self.service, name="Malaria RDT", result_type="choice",
            choices="Negative,Positive", abnormal_choices="Positive", sort_order=1,
        )

    def _user(self, name, role):
        user = get_user_model().objects.create_user(name, password="x")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name="Main")
        return user

    def _order(self):
        self.client.force_authenticate(self.doctor)
        res = self.client.post(
            "/api/clinic/orders/",
            {"encounter": self.encounter.id, "order_type": "lab", "details": "x", "service": self.service.id},
        )
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["id"]

    def test_order_exposes_parameters(self, _perm):
        oid = self._order()
        self.client.force_authenticate(self.lab)
        res = self.client.get(f"/api/clinic/orders/{oid}/")
        self.assertEqual([p["name"] for p in res.data["test_parameters"]], ["Hemoglobin", "Malaria RDT"])

    def test_complete_flags_values(self, _perm):
        oid = self._order()
        self.client.force_authenticate(self.lab)
        params = {p.name: str(p.id) for p in LabTestParameter.objects.all()}
        res = self.client.post(
            f"/api/clinic/orders/{oid}/complete/",
            {"values": {params["Hemoglobin"]: "10", params["Malaria RDT"]: "Positive"}},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        flags = {v["name"]: v["flag"] for v in res.data["result_values"]}
        self.assertEqual(flags, {"Hemoglobin": "low", "Malaria RDT": "abnormal"})
        self.assertEqual(ClinicalOrder.objects.get(pk=oid).status, "Completed")
        self.assertIn("Hemoglobin: 10", res.data["result_text"])

    def test_missing_or_invalid_value_rejected(self, _perm):
        oid = self._order()
        self.client.force_authenticate(self.lab)
        hb = LabTestParameter.objects.get(name="Hemoglobin")
        res = self.client.post(
            f"/api/clinic/orders/{oid}/complete/", {"values": {str(hb.id): "abc"}}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ClinicalOrder.objects.get(pk=oid).status, "PaymentApproved")

    def test_free_text_service_still_works(self, _perm):
        self.service.parameters.all().delete()
        oid = self._order()
        self.client.force_authenticate(self.lab)
        res = self.client.post(f"/api/clinic/orders/{oid}/complete/", {"result_text": "ok"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["result_values"], [])

    def test_only_manager_edits_parameters(self, _perm):
        body = [{"name": "ESR", "result_type": "numeric", "unit": "mm/hr", "ref_high": "20"}]
        self.client.force_authenticate(self.doctor)
        self.assertEqual(
            self.client.put(f"/api/clinic/services/{self.service.id}/parameters/", body, format="json").status_code, 403
        )
        self.client.force_authenticate(self.manager)
        res = self.client.put(f"/api/clinic/services/{self.service.id}/parameters/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([p["name"] for p in res.data], ["ESR"])


@mock.patch("tenants.permissions.TenantBillingAccessPermission.has_permission", return_value=True)
class RadiologyReportTests(APITestCase):
    def setUp(self):
        for name, role in (("doc", "doctor"), ("rad", "radiology")):
            user = get_user_model().objects.create_user(name, password="x")
            UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name="Main")
        self.doc = get_user_model().objects.get(username="doc")
        self.rad = get_user_model().objects.get(username="rad")
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="M2", full_name="P")
        enc = Encounter.objects.create(
            clinic_tin=TIN, branch_name="Main", number="E2", patient=patient, arrival_type="new"
        )
        svc = BillableService.objects.create(
            clinic_tin=TIN, branch_name="Main", code="CXR", name="Chest X-ray", department="Radiology",
            service_type="radiology", unit_price=200, requires_payment_before_work=False,
        )
        self.client.force_authenticate(self.doc)
        with mock.patch(
            "tenants.permissions.TenantBillingAccessPermission.has_permission", return_value=True
        ):
            res = self.client.post(
                "/api/clinic/orders/",
                {"encounter": enc.id, "order_type": "radiology", "details": "x", "service": svc.id},
            )
        self.assertEqual(res.status_code, 201, res.data)
        self.oid = res.data["id"]
        self.client.force_authenticate(self.rad)

    def test_report_saved_and_returned(self, _perm):
        res = self.client.post(
            f"/api/clinic/orders/{self.oid}/complete/",
            {
                "findings": "Clear lung fields.",
                "impression": "No acute disease.",
                "tag": "normal",
                "attachments": [{"url": "https://res.cloudinary.com/x/a.jpg", "name": "a.jpg"}],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["radiology_report"]["tag"], "normal")
        self.assertEqual(len(res.data["radiology_report"]["attachments"]), 1)
        self.assertEqual(res.data["status"], "Completed")

    def test_requires_findings_impression_tag(self, _perm):
        for body in ({"impression": "x", "tag": "normal"}, {"findings": "x", "impression": "y"}):
            res = self.client.post(f"/api/clinic/orders/{self.oid}/complete/", body, format="json")
            self.assertEqual(res.status_code, 400)

    def test_rejects_non_https_attachment(self, _perm):
        res = self.client.post(
            f"/api/clinic/orders/{self.oid}/complete/",
            {"findings": "a", "impression": "b", "tag": "abnormal",
             "attachments": [{"url": "javascript:alert(1)"}]},
            format="json",
        )
        self.assertEqual(res.status_code, 400)


@mock.patch("tenants.permissions.TenantBillingAccessPermission.has_permission", return_value=True)
class ReportsRangeTests(APITestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("mgr2", password="x")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role="manager", branch_name="Main")
        doc = get_user_model().objects.create_user("doc2", password="x")
        UserProfile.objects.create(user=doc, clinic_tin=TIN, role="doctor", branch_name="Main")
        self.mgr, self.doc = user, doc
        patient = Patient.objects.create(clinic_tin=TIN, branch_name="Main", mrn="R1", full_name="P")
        Encounter.objects.create(
            clinic_tin=TIN, branch_name="Main", number="R-1", patient=patient, arrival_type="new"
        )

    def test_custom_range_filters_and_labels(self, _perm):
        self.client.force_authenticate(self.mgr)
        res = self.client.get("/api/clinic/reports/", {"from": "2020-01-01", "to": "2020-01-31"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["period"], "custom")
        self.assertEqual((res.data["start_date"], res.data["end_date"]), ("2020-01-01", "2020-01-31"))
        self.assertEqual(res.data["summary"]["visits"], 0)
        self.assertEqual(res.data["visits_by_day"], [])

    def test_range_including_today_counts_visit(self, _perm):
        from django.utils import timezone

        today = timezone.localdate().isoformat()
        self.client.force_authenticate(self.mgr)
        res = self.client.get("/api/clinic/reports/", {"from": today, "to": today})
        self.assertEqual(res.data["summary"]["visits"], 1)
        self.assertEqual(res.data["visits_by_day"][0]["count"], 1)

    def test_invalid_ranges_rejected(self, _perm):
        self.client.force_authenticate(self.mgr)
        for params in ({"from": "nope", "to": "2020-01-02"},
                       {"from": "2020-02-01", "to": "2020-01-01"},
                       {"from": "2015-01-01", "to": "2020-01-01"}):
            self.assertEqual(self.client.get("/api/clinic/reports/", params).status_code, 400, params)

    def test_non_manager_forbidden(self, _perm):
        self.client.force_authenticate(self.doc)
        self.assertEqual(self.client.get("/api/clinic/reports/", {"from": "2020-01-01"}).status_code, 403)
