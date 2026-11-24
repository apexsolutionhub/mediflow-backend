"""Offline-sync phase 6: conflicts reach the clinic manager and can be reviewed; backups can be verified."""
import gzip
import tempfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from api.models import UserProfile
from clinic.models import Patient, SyncConflict
from clinic.sync_engine import PULL, PUSH, Scope, apply_scope, export_scope
from clinic.tests_backup import GOOD, put_dump, NOW
from clinic.tests_sync_phase1 import BRANCH, CLOUD, NODE, SCOPE, TIN, SyncCase, _patient


def _both_edit_a_patient(case):
    patient = _patient(CLOUD, phone="0911")
    case.seed_node()
    local = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
    local.phone = "0922"
    local.save(using=NODE)
    Patient.objects.filter(pk=patient.pk).update(phone="0933", updated_at=timezone.now())
    return patient


class ConflictFlowTests(SyncCase):
    def test_a_conflict_the_cloud_found_is_shown_to_the_clinic_manager(self):
        _both_edit_a_patient(self)
        run = self.sync()
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)
        on_server = SyncConflict.objects.using(NODE).get(entity="patients")
        self.assertEqual(on_server.side, "cloud")
        self.assertEqual(on_server.incoming["phone"], "0922")  # what this server tried to send
        self.assertEqual(on_server.existing["phone"], "0933")  # what the cloud kept
        self.assertEqual(SyncConflict.objects.get(entity="patients").side, "cloud")  # and the cloud has its own record
        # After the pull both sides agree on the cloud copy.
        self.assertEqual(Patient.objects.using(NODE).get(mrn="1").phone, "0933")

    def test_the_same_conflict_is_not_logged_twice(self):
        patient = _both_edit_a_patient(self)
        self.sync()
        # Edit both again before the next sync: the open entry is updated, not duplicated.
        local = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        local.phone = "0944"
        local.save(using=NODE)
        Patient.objects.filter(pk=patient.pk).update(phone="0955", updated_at=timezone.now())
        self.sync()
        self.assertEqual(SyncConflict.objects.using(NODE).filter(entity="patients", resolved_at__isnull=True).count(), 1)
        self.assertEqual(SyncConflict.objects.filter(entity="patients", resolved_at__isnull=True).count(), 1)
        self.assertEqual(SyncConflict.objects.using(NODE).get(entity="patients").incoming["phone"], "0944")

    def test_a_reviewed_conflict_opens_a_new_one_if_the_row_conflicts_again(self):
        patient = _both_edit_a_patient(self)
        self.sync()
        SyncConflict.objects.using(NODE).update(resolved_at=timezone.now())
        local = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        local.phone = "0966"
        local.save(using=NODE)
        Patient.objects.filter(pk=patient.pk).update(phone="0977", updated_at=timezone.now())
        self.sync()
        self.assertEqual(SyncConflict.objects.using(NODE).filter(entity="patients").count(), 2)

    def test_each_side_says_which_copy_was_kept(self):
        patient = _both_edit_a_patient(self)
        rows = export_scope(SCOPE, db=CLOUD)
        apply_scope(SCOPE, {"patients": [{**rows["patients"][0], "data": {**rows["patients"][0]["data"], "phone": "0001"}}]}, PUSH, db=CLOUD)
        push_note = SyncConflict.objects.get(entity="patients").note
        self.assertIn("cloud copy was kept", push_note)
        # A pull that meets a row changed on the server during the sync keeps the server copy.
        rows = export_scope(SCOPE, db=CLOUD)
        incoming = {**rows["patients"][0], "data": {**rows["patients"][0]["data"], "phone": "0002"}}
        local = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        local.phone = "0888"
        local.save(using=NODE)
        apply_scope(SCOPE, {"patients": [incoming]}, PULL, db=NODE, local_cutoff=timezone.now() - timedelta(minutes=1))
        pull_note = SyncConflict.objects.using(NODE).filter(side="node").first().note
        self.assertIn("clinic server copy was kept", pull_note)
        self.assertEqual(Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid).phone, "0888")


class ConflictReviewApiTests(TestCase):
    def setUp(self):
        self.mgr = get_user_model().objects.create_user("cmgr", password="x")
        UserProfile.objects.create(user=self.mgr, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        self.nurse = get_user_model().objects.create_user("cnurse", password="x")
        UserProfile.objects.create(user=self.nurse, clinic_tin=TIN, role="nurse", branch_name=BRANCH)
        self.conflict = SyncConflict.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, entity="patients", row_uuid="u-1", label="Abebe", side="cloud",
            incoming={"phone": "0922", "full_name": "Abebe K", "age": 30},
            existing={"phone": "0933", "full_name": "Abebe K", "age": 30},
            note="Changed on both sides.",
        )

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_the_list_shows_only_what_differs_and_which_copy_is_live(self):
        body = self.client_for(self.mgr).get("/api/sync/conflicts/").json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["kept"], "cloud")
        self.assertEqual(body[0]["differences"], [{"field": "phone", "server": "0922", "cloud": "0933"}])

    def test_for_a_server_side_conflict_the_roles_are_swapped(self):
        SyncConflict.objects.create(
            clinic_tin=TIN, branch_name=BRANCH, entity="patients", row_uuid="u-2", label="Sara", side="node",
            incoming={"phone": "0001"}, existing={"phone": "0002"},
        )
        body = {c["label"]: c for c in self.client_for(self.mgr).get("/api/sync/conflicts/").json()}
        self.assertEqual(body["Sara"]["kept"], "server")
        self.assertEqual(body["Sara"]["differences"], [{"field": "phone", "server": "0002", "cloud": "0001"}])

    def test_marking_a_conflict_reviewed_records_who_and_why(self):
        res = self.client_for(self.mgr).post(
            f"/api/sync/conflicts/{self.conflict.id}/resolve/", {"note": "Re-entered the new number"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.conflict.refresh_from_db()
        self.assertIsNotNone(self.conflict.resolved_at)
        self.assertEqual((self.conflict.resolved_by, self.conflict.resolution_note), ("cmgr", "Re-entered the new number"))
        self.assertEqual(self.client_for(self.mgr).get("/api/sync/conflicts/").json(), [])
        shown = self.client_for(self.mgr).get("/api/sync/conflicts/", {"include": "resolved"}).json()
        self.assertEqual(shown[0]["resolved_by"], "cmgr")

    def test_a_conflict_is_reviewed_once(self):
        client = self.client_for(self.mgr)
        client.post(f"/api/sync/conflicts/{self.conflict.id}/resolve/", {}, format="json")
        self.assertEqual(client.post(f"/api/sync/conflicts/{self.conflict.id}/resolve/", {}, format="json").status_code, 400)

    def test_only_the_branch_manager_can_review(self):
        self.assertEqual(
            self.client_for(self.nurse).post(f"/api/sync/conflicts/{self.conflict.id}/resolve/", {}, format="json").status_code, 403
        )
        self.assertEqual(self.client_for(self.nurse).get("/api/sync/conflicts/").status_code, 403)
        other = get_user_model().objects.create_user("omgr", password="x")
        UserProfile.objects.create(user=other, clinic_tin="OTHER-TIN", role="manager", branch_name=BRANCH)
        res = self.client_for(other).post(f"/api/sync/conflicts/{self.conflict.id}/resolve/", {}, format="json")
        self.assertEqual(res.status_code, 404)
        self.conflict.refresh_from_db()
        self.assertIsNone(self.conflict.resolved_at)


class VerifyBackupCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name) / "backups"
        self.second = Path(self.tmp.name) / "usb"
        override = override_settings(BACKUP_DIR=str(self.main), BACKUP_COPY_DIR=str(self.second))
        override.enable()
        self.addCleanup(override.disable)

    def run_command(self, *args):
        out = StringIO()
        call_command("verify_backup", *args, stdout=out)
        return out.getvalue()

    def test_good_backups_pass_in_both_places(self):
        put_dump(self.main, NOW)
        put_dump(self.second, NOW)
        out = self.run_command()
        self.assertIn("The newest backup checks out", out)
        self.assertIn("Second location", out)

    def test_a_damaged_newest_backup_is_an_error(self):
        put_dump(self.main, NOW).write_bytes(b"broken")
        with self.assertRaisesMessage(CommandError, "damaged"):
            self.run_command()

    def test_no_backups_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "no backups"):
            self.run_command()

    def test_one_file_can_be_checked(self):
        good = put_dump(self.main, NOW)
        self.assertIn("ok", self.run_command("--file", str(good)))
        bad = self.main / "x.sql.gz"
        with gzip.open(bad, "wb") as handle:
            handle.write(b"".join(GOOD[:2]))  # no "Dump completed"
        with self.assertRaises(CommandError):
            self.run_command("--file", str(bad))
