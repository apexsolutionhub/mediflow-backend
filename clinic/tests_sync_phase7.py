"""Offline-sync phase 7: reminders, cloud-only chat on a clinic server, local password change and bell."""
from datetime import datetime, timedelta
from datetime import timezone as tz
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import SyncState
from clinic.sync_client import LocalTransport, TransportError
from clinic.sync_cloud import issue_node
from clinic.sync_gate import sync_reminder
from tenants.models import TenantAccount, TenantFeedbackMessage, TenantFeedbackThread

TIN = "P7-TIN"
BRANCH = "Main"
DUE = "clinic.sync_gate.days_until_due"
CALL = "clinic.sync_billing.cloud_call"
PERM = "tenants.permissions.TenantBillingAccessPermission.has_permission"
NOW = datetime(2026, 11, 24, 8, 0, 0, tzinfo=tz.utc)
NODE_SETTINGS = dict(NODE_MODE="clinic", CLOUD_API_URL="https://cloud.example/api", NODE_TOKEN="t")


def _synced(ago: timedelta, **over):
    return SyncState.objects.update_or_create(
        clinic_tin=TIN, branch_name=BRANCH, defaults={"last_successful_sync_at": NOW - ago, **over}
    )[0]


@override_settings(NODE_MODE="clinic")
class ReminderTests(TestCase):
    def setUp(self):
        self.tenant = TenantAccount.objects.create(clinic_tin=TIN, ops_mode="offline", is_illustration=True)

    def reminder(self, due_in=30):
        with mock.patch(DUE, return_value=due_in):
            return sync_reminder(self.tenant, BRANCH, now=NOW)

    def test_last_evenings_sync_is_a_normal_morning_and_needs_no_reminder(self):
        _synced(timedelta(hours=14))  # yesterday 18:00
        r = self.reminder()
        self.assertEqual((r["due"], r["warn"], r["reason"]), (False, False, None))

    def test_a_missed_night_triggers_the_morning_reminder(self):
        _synced(timedelta(days=2))
        r = self.reminder()
        self.assertEqual((r["due"], r["reason"], r["overdue"]), (True, "missed_last_night", False))

    def test_never_synced_is_overdue_and_due(self):
        r = self.reminder()
        self.assertEqual((r["due"], r["overdue"], r["reason"]), (True, True, "overdue"))

    def test_the_warning_starts_one_day_before_staff_are_locked_out(self):
        _synced(timedelta(days=5))
        self.assertEqual((self.reminder()["warn"], self.reminder()["days_left"]), (False, 2))
        _synced(timedelta(days=6))
        r = self.reminder()
        self.assertEqual((r["warn"], r["days_left"], r["due"]), (True, 1, True))
        _synced(timedelta(days=8))
        r = self.reminder()
        self.assertEqual((r["overdue"], r["warn"], r["days_left"]), (True, True, None))

    def test_near_the_due_date_the_two_day_rule_warns_sooner(self):
        _synced(timedelta(days=1))
        r = self.reminder(due_in=3)
        self.assertEqual((r["max_days"], r["days_left"], r["warn"]), (2, 1, True))

    def test_online_clinics_and_the_cloud_get_nothing(self):
        self.tenant.ops_mode = "online"
        self.assertFalse(self.reminder()["applies"])
        self.tenant.ops_mode = "offline"
        with override_settings(NODE_MODE="cloud"):
            self.assertFalse(self.reminder()["applies"])

    def test_the_status_endpoint_carries_it_for_the_manager(self):
        user = get_user_model().objects.create_user("rmgr", password="x")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        client = APIClient()
        client.force_authenticate(user)
        body = client.get("/api/sync/status/").json()
        self.assertTrue(body["freshness"]["applies"])
        self.assertTrue(body["freshness"]["overdue"])  # never synced


class ChatTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("chatter", password="x")
        UserProfile.objects.create(user=self.user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.node, self.token = issue_node(TIN, BRANCH, "pc")
        self.transport = LocalTransport(self.node)

    def to_cloud(self, path, body):
        return self.transport.call(path, body)

    @override_settings(NODE_MODE="cloud")
    def test_on_the_cloud_the_chat_works_as_before(self):
        res = self.client.post("/api/billing/feedback/send/", {"body": "hello"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.client.get("/api/billing/feedback/").json()["messages"][0]["body"], "hello")
        self.assertEqual(self.client.get("/api/billing/feedback/unread/").json(), {"unread_count": 0})

    @override_settings(**NODE_SETTINGS)
    def test_a_clinic_server_sends_through_the_cloud(self):
        with mock.patch(CALL, side_effect=self.to_cloud) as call:
            res = self.client.post("/api/billing/feedback/send/", {"body": "from the server"}, format="json")
            thread = self.client.get("/api/billing/feedback/").json()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(call.call_args_list[0].args[0], "feedback/send")
        stored = TenantFeedbackMessage.objects.get(body="from the server")
        self.assertEqual((stored.sender_username, stored.thread.pharmacy_tin), ("chatter", TIN))
        self.assertEqual(thread["messages"][0]["body"], "from the server")

    @override_settings(**NODE_SETTINGS)
    def test_with_no_cloud_nothing_is_sent_or_kept_and_the_chat_says_so(self):
        with mock.patch(CALL, side_effect=TransportError("unreachable")):
            send = self.client.post("/api/billing/feedback/send/", {"body": "lost?"}, format="json")
            thread = self.client.get("/api/billing/feedback/")
            unread = self.client.get("/api/billing/feedback/unread/")
        self.assertEqual((send.status_code, send.json()["code"]), (503, "cloud_unreachable"))
        self.assertEqual((thread.status_code, thread.json()["code"]), (503, "cloud_unreachable"))
        # The unread poll is a state, not an error.
        self.assertEqual((unread.status_code, unread.json()), (200, {"unread_count": 0, "cloud_unreachable": True}))
        # Never queued, never stored on the clinic server.
        self.assertEqual(TenantFeedbackMessage.objects.count(), 0)
        self.assertEqual(TenantFeedbackThread.objects.count(), 0)

    @override_settings(**NODE_SETTINGS)
    def test_a_refusal_from_the_cloud_is_passed_on_not_called_an_outage(self):
        with mock.patch(CALL, side_effect=self.to_cloud):
            res = self.client.post("/api/billing/feedback/send/", {"body": "  "}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_the_cloud_endpoints_use_the_tokens_clinic_not_the_payload(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Node {self.token}")
        res = client.post(
            "/api/sync/feedback/send/",
            {"body": "x", "username": "mgr", "tin": "SOMEONE-ELSE"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(TenantFeedbackMessage.objects.get().thread.pharmacy_tin, TIN)
        self.assertEqual(client.post("/api/sync/feedback/thread/", {}, format="json").status_code, 200)
        self.assertEqual(client.post("/api/sync/feedback/unread/", {}, format="json").status_code, 200)
        self.assertIn(APIClient().post("/api/sync/feedback/send/", {"body": "x"}, format="json").status_code, (401, 403))


@override_settings(**NODE_SETTINGS)
class LocalOnTheClinicServerTests(TestCase):
    """Password change and the notification bell must work with no cloud at all (contract section 6.3)."""

    def setUp(self):
        self.user = get_user_model().objects.create_user("localmgr", password="old-pass-1")
        UserProfile.objects.create(user=self.user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_changing_a_password_is_local_and_immediate(self):
        with mock.patch(CALL, side_effect=AssertionError("must not call the cloud")):
            res = self.client.post(
                "/api/user/change_password/", {"old_password": "old-pass-1", "new_password": "new-pass-2"}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("new-pass-2"))

    def test_the_notification_bell_reads_the_local_dashboard(self):
        with mock.patch(CALL, side_effect=AssertionError("must not call the cloud")), mock.patch(PERM, return_value=True):
            res = self.client.get("/api/clinic/dashboard/")
        self.assertEqual(res.status_code, 200, res.content)
