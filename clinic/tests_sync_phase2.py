"""Offline-sync phase 2: the freshness gate on login and API, and billing during lockout."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from api.models import UserProfile
from clinic.models import SyncState
from clinic.sync_client import LocalTransport, TransportError, run_sync
from clinic.sync_cloud import issue_node
from clinic.sync_engine import Scope
from clinic.sync_gate import STAFF_MESSAGE, sync_gate
from tenants.billing import PAYMENT_CHANNELS
from tenants.models import TenantAccount, TenantPaymentSubmission

TIN = "SYNC-P2"
BRANCH = "Main"
SCOPE = Scope(TIN, BRANCH)
DUE = "clinic.sync_gate.days_until_due"
CHANNEL = sorted(PAYMENT_CHANNELS)[0]


def _user(name, role):
    user = get_user_model().objects.create_user(name, password="pw")
    UserProfile.objects.create(user=user, clinic_tin=TIN, role=role, branch_name=BRANCH)
    return user


def _synced(days_ago, **over):
    return SyncState.objects.update_or_create(
        clinic_tin=TIN,
        branch_name=BRANCH,
        defaults={"last_successful_sync_at": timezone.now() - timedelta(days=days_ago), **over},
    )[0]


@override_settings(NODE_MODE="clinic")
class FreshnessRuleTests(APITestCase):
    def setUp(self):
        self.tenant = TenantAccount.objects.create(clinic_tin=TIN, ops_mode="offline", is_illustration=True)

    def gate(self, due_in):
        with mock.patch(DUE, return_value=due_in):
            return sync_gate(self.tenant, BRANCH)

    def test_far_from_due_allows_seven_days(self):
        _synced(7)
        self.assertFalse(self.gate(30)["overdue"])
        _synced(8)
        gate = self.gate(30)
        self.assertEqual((gate["overdue"], gate["reason"], gate["max_days"]), (True, "stale", 7))

    def test_within_a_week_of_due_allows_two_days(self):
        _synced(2)
        self.assertFalse(self.gate(7)["overdue"])
        _synced(3)
        gate = self.gate(7)
        self.assertEqual((gate["overdue"], gate["max_days"]), (True, 2))

    def test_the_boundary_is_seven_days_out(self):
        _synced(3)
        self.assertFalse(self.gate(8)["overdue"])  # 8 days to due: the 7-day allowance applies
        self.assertTrue(self.gate(7)["overdue"])

    def test_no_due_date_uses_the_long_allowance(self):
        _synced(6)
        self.assertFalse(self.gate(None)["overdue"])

    def test_never_synced_is_overdue(self):
        gate = self.gate(30)
        self.assertEqual((gate["overdue"], gate["reason"]), (True, "never_synced"))

    def test_a_restore_blocks_even_a_fresh_sync_until_the_next_one(self):
        _synced(0, restore_pending=True)
        gate = self.gate(30)
        self.assertEqual((gate["overdue"], gate["reason"]), (True, "restored"))

    def test_online_tenants_and_the_cloud_are_never_gated(self):
        self.tenant.ops_mode = "online"
        self.assertFalse(self.gate(30)["applies"])
        self.tenant.ops_mode = "offline"
        with override_settings(NODE_MODE="cloud"):
            with mock.patch(DUE, return_value=30):
                self.assertFalse(sync_gate(self.tenant, BRANCH)["applies"])


@override_settings(NODE_MODE="clinic")
class GateEnforcementTests(APITestCase):
    def setUp(self):
        self.tenant = TenantAccount.objects.create(clinic_tin=TIN, ops_mode="offline", is_illustration=True)
        self.manager = _user("gmgr", "manager")
        self.doctor = _user("gdoc", "doctor")

    def login(self, name):
        return APIClient().post("/api/auth/login/", {"username": name, "password": "pw"}, format="json")

    def test_staff_cannot_sign_in_when_overdue(self):
        _synced(10)
        res = self.login("gdoc")
        self.assertEqual(res.status_code, 401)
        self.assertIn(STAFF_MESSAGE, str(res.json()))

    def test_the_manager_signs_in_but_only_for_sync_and_billing(self):
        _synced(10)
        res = self.login("gmgr")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["access_mode"], "sync_required")
        self.assertTrue(res.json()["sync_gate"]["overdue"])

    def test_everyone_signs_in_normally_after_a_fresh_sync(self):
        _synced(1)
        self.assertEqual(self.login("gdoc").status_code, 200)
        res = self.login("gmgr")
        self.assertEqual(res.json()["access_mode"], "full")

    def test_a_staff_session_started_earlier_is_stopped_at_the_api(self):
        client = APIClient()
        client.force_authenticate(self.doctor)
        _synced(1)
        self.assertEqual(client.get("/api/clinic/patients/").status_code, 200)
        _synced(10)
        res = client.get("/api/clinic/patients/")
        self.assertEqual(res.status_code, 403)
        self.assertIn("synced", str(res.json()))

    def test_the_manager_keeps_sync_and_billing_but_loses_clinic_pages(self):
        _synced(10)
        client = APIClient()
        client.force_authenticate(self.manager)
        self.assertEqual(client.get("/api/clinic/patients/").status_code, 403)
        self.assertEqual(client.get("/api/sync/status/").status_code, 200)
        me = client.get("/api/billing/me/")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["access_mode"], "sync_required")

    def test_a_successful_sync_clears_the_restore_flag(self):
        node_db = "node"
        TenantAccount.objects.using(node_db)  # table exists on the throwaway node database
        cloud_node, _token = issue_node(TIN, BRANCH, "pc")
        SyncState.objects.using(node_db).create(clinic_tin=TIN, branch_name=BRANCH, restore_pending=True)
        run = run_sync(SCOPE, LocalTransport(cloud_node), kind="seed", db=node_db)
        self.assertEqual(run.pull_status, "ok", run.error_message)
        self.assertFalse(SyncState.objects.using(node_db).get(clinic_tin=TIN).restore_pending)

    databases = {"default", "node"}


class BillingLockoutTests(APITestCase):
    databases = {"default", "node"}

    def setUp(self):
        # Trial over and no payment: the manager is in the payment portal, staff are locked out.
        self.tenant = TenantAccount.objects.create(
            clinic_tin=TIN,
            ops_mode="offline",
            setup_fee_approved=False,
            free_trial_ends_at=timezone.now() - timedelta(days=3),
        )
        self.manager = _user("bmgr", "manager")
        self.cloud_node, _ = issue_node(TIN, BRANCH, "pc")
        self.client.force_authenticate(self.manager)
        self.transport = LocalTransport(self.cloud_node)

    def call_cloud(self, path, body):
        return self.transport.call(path, body)

    @override_settings(NODE_MODE="clinic", CLOUD_API_URL="https://cloud.example", NODE_TOKEN="t")
    def test_lockout_status_comes_from_the_cloud(self):
        with mock.patch("clinic.sync_billing.cloud_call", side_effect=self.call_cloud) as call:
            res = self.client.get("/api/billing/me/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["live"])
        self.assertEqual(res.json()["access_mode"], "payment_portal")
        call.assert_called_once()

    @override_settings(NODE_MODE="clinic", CLOUD_API_URL="https://cloud.example", NODE_TOKEN="t")
    def test_lockout_payment_proof_is_filed_on_the_cloud(self):
        with mock.patch("clinic.sync_billing.cloud_call", side_effect=self.call_cloud):
            res = self.client.post(
                "/api/billing/submit-payment/",
                {"payment_channel": CHANNEL, "transaction_ref": "REF-LOCKOUT-1"},
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(
            TenantPaymentSubmission.objects.filter(clinic_tin=TIN, transaction_ref="REF-LOCKOUT-1").exists()
        )

    @override_settings(NODE_MODE="clinic", CLOUD_API_URL="https://cloud.example", NODE_TOKEN="t")
    def test_lockout_payment_proof_needs_the_internet(self):
        with mock.patch("clinic.sync_billing.cloud_call", side_effect=TransportError("down")):
            res = self.client.post(
                "/api/billing/submit-payment/",
                {"payment_channel": CHANNEL, "transaction_ref": "REF-LOCKOUT-2"},
                format="json",
            )
        self.assertEqual(res.status_code, 503)
        self.assertFalse(TenantPaymentSubmission.objects.filter(transaction_ref="REF-LOCKOUT-2").exists())

    def test_a_server_without_the_pricing_catalog_uses_the_fees_in_the_tenant_record(self):
        # The catalog table belongs to the Apex admin; a clinic database does not have it.
        from clinic.sync_engine import export_tenant
        from tenants.billing import effective_tenant_fees

        self.assertEqual(effective_tenant_fees(self.tenant), (15000, 5000))
        snapshot = export_tenant(TIN)
        self.assertEqual((snapshot["setup_fee_etb"], snapshot["quarterly_fee_etb"]), (15000, 5000))

    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_never_forwards(self):
        with mock.patch("clinic.sync_billing.cloud_call") as call:
            res = self.client.get("/api/billing/me/")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("live", res.json())
        call.assert_not_called()

    def test_a_warning_window_proof_travels_with_the_next_sync(self):
        node = "node"
        TenantAccount.objects.filter(pk=self.tenant.pk).update(
            setup_fee_approved=True, free_trial_ends_at=None
        )
        run = run_sync(SCOPE, self.transport, kind="seed", db=node)
        self.assertEqual(run.pull_status, "ok", run.error_message)
        TenantPaymentSubmission.objects.using(node).create(
            clinic_tin=TIN, payment_kind="quarterly", amount_etb=5000,
            payment_channel=CHANNEL, transaction_ref="REF-WARN-1",
        )

        run = run_sync(SCOPE, self.transport, db=node)
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)
        cloud_row = TenantPaymentSubmission.objects.get(clinic_tin=TIN, transaction_ref="REF-WARN-1")
        self.assertEqual(cloud_row.status, "pending")

        # Apex approves on the cloud; the next sync brings the verdict back to the node.
        TenantPaymentSubmission.objects.filter(pk=cloud_row.pk).update(status="approved")
        run_sync(SCOPE, self.transport, db=node)
        local = TenantPaymentSubmission.objects.using(node).get(transaction_ref="REF-WARN-1")
        self.assertEqual(local.status, "approved")

        # Sending the same proof again does not file a second one.
        run_sync(SCOPE, self.transport, db=node)
        self.assertEqual(TenantPaymentSubmission.objects.filter(transaction_ref="REF-WARN-1").count(), 1)
