"""A branch created without a TIN gets its own generated one, never the organization's."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import ClinicBranch
from tenants.models import TenantAccount

ORG = "ORG-TIN-1"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"


class BranchTinTests(TestCase):
    # The pricing table belongs to the Apex admin (managed elsewhere), so a fresh test database has none.
    _made = []

    @classmethod
    def setUpClass(cls):
        from django.db import connection

        from tenants.models import SubscriptionPricingRule

        existing = set(connection.introspection.table_names())
        cls._made = []
        with connection.schema_editor() as editor:
            if SubscriptionPricingRule._meta.db_table not in existing:
                editor.create_model(SubscriptionPricingRule)
                cls._made.append(SubscriptionPricingRule)
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        from django.db import connection

        super().tearDownClass()
        with connection.schema_editor() as editor:
            for model in reversed(cls._made):
                editor.delete_model(model)

    def setUp(self):
        user = get_user_model().objects.create_user("owner1", password="x")
        UserProfile.objects.create(user=user, clinic_tin=ORG, role="manager", branch_name="Main")
        self.client = APIClient()
        self.client.force_authenticate(user)
        patcher = mock.patch(PERM, return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, **body):
        return self.client.post("/api/clinic/branches/", {"address": "x", **body}, format="json")

    def test_a_satellite_branch_without_a_tin_gets_its_own(self):
        self.make(name="HQ", is_main=True, branch_tin=ORG)
        res = self.make(name="Bole")
        self.assertEqual(res.status_code, 201, res.content)
        tin = ClinicBranch.objects.get(clinic_tin=ORG, name="Bole").branch_tin
        self.assertTrue(tin.startswith("GEN-"))
        self.assertNotEqual(tin, ORG)
        # It is a clinic account of its own, so it has its own billing, mode and clinic-server token.
        self.assertTrue(TenantAccount.objects.filter(clinic_tin=tin).exists())

    def test_two_blank_branches_never_share_a_tin(self):
        self.make(name="HQ", is_main=True, branch_tin=ORG)
        self.make(name="Bole")
        self.make(name="Piassa")
        tins = set(ClinicBranch.objects.filter(clinic_tin=ORG, is_main=False).values_list("branch_tin", flat=True))
        self.assertEqual(len(tins), 2)

    def test_a_tin_the_owner_types_is_kept(self):
        self.make(name="HQ", is_main=True, branch_tin=ORG)
        self.make(name="Bole", branch_tin="REAL-123")
        self.assertEqual(ClinicBranch.objects.get(clinic_tin=ORG, name="Bole").branch_tin, "REAL-123")

    def test_the_main_branch_without_a_tin_uses_the_organizations(self):
        self.make(name="HQ", is_main=True)
        self.assertEqual(ClinicBranch.objects.get(clinic_tin=ORG, name="HQ").branch_tin, ORG)
