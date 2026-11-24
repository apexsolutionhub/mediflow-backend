"""Offline-sync phase 1: a clinic node (throwaway "node" database) syncing with the cloud ("default")."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from api.models import UserProfile
from clinic import ledger
from clinic.models import (
    Encounter,
    Medicine,
    MedicineDamage,
    Patient,
    PaymentTransaction,
    StockMovement,
    SyncConflict,
    SyncRun,
    SyncState,
)
from clinic.sync_client import LocalTransport, TransportError, run_sync
from clinic.sync_cloud import issue_node
from clinic.sync_engine import PULL, PUSH, Scope, SyncError, apply_scope, export_scope
from tenants.models import TenantAccount, TenantOpsModeChangeRequest

TIN = "SYNC-P1"
OTHER = "SYNC-OTHER"
BRANCH = "Main"
SCOPE = Scope(TIN, BRANCH)
CLOUD, NODE = "default", "node"


def _med(db, **over):
    base = dict(
        clinic_tin=TIN, branch_name=BRANCH, name="Paracetamol", batch_number="B1", unit_of_measure="box",
        on_hand=10, unit_price=100, cost=60, pack_size=10, loose_unit="Tablet",
    )
    base.update(over)
    med = Medicine.objects.using(db).create(**base)
    ledger.record_movement(med, "opening", "test")
    return med


def _patient(db, mrn="1", **over):
    base = dict(clinic_tin=TIN, branch_name=BRANCH, mrn=mrn, full_name=f"Patient {mrn}")
    base.update(over)
    return Patient.objects.using(db).create(**base)


def _encounter(db, patient, number="E1", **over):
    base = dict(clinic_tin=TIN, branch_name=BRANCH, number=number, patient=patient, arrival_type="new")
    base.update(over)
    return Encounter.objects.using(db).create(**base)


class SyncCase(APITestCase):
    databases = {"default", "node"}

    def setUp(self):
        self.tenant = TenantAccount.objects.create(clinic_tin=TIN, ops_mode="online")
        self.cloud_node, self.token = issue_node(TIN, BRANCH, "Reception PC")
        self.transport = LocalTransport(self.cloud_node)

    def sync(self, kind="sync", **kw):
        return run_sync(SCOPE, self.transport, kind=kind, db=NODE, **kw)

    def seed_node(self):
        run = self.sync("seed")
        self.assertEqual((run.push_status, run.pull_status), ("skipped", "ok"), run.error_message)
        return run

    def stock(self, db, med_uuid):
        m = Medicine.objects.using(db).get(sync_uuid=med_uuid)
        return ledger.stock_units(m)


class SeedTests(SyncCase):
    def test_seed_copies_the_branch_with_the_same_ids(self):
        patient = _patient(CLOUD)
        _encounter(CLOUD, patient)
        med = _med(CLOUD)
        staff = get_user_model().objects.create_user("doc", password="pw")
        UserProfile.objects.create(user=staff, clinic_tin=TIN, role="doctor", branch_name=BRANCH)

        self.seed_node()

        node_patient = Patient.objects.using(NODE).get(mrn="1")
        self.assertEqual(node_patient.sync_uuid, patient.sync_uuid)
        self.assertEqual(Encounter.objects.using(NODE).get(number="E1").patient_id, node_patient.id)
        self.assertEqual(self.stock(NODE, med.sync_uuid), 100)
        node_staff = get_user_model().objects.using(NODE).get(username="doc")
        self.assertEqual(node_staff.password, staff.password)  # the hash travels, never a plain password
        self.assertTrue(TenantAccount.objects.using(NODE).filter(clinic_tin=TIN).exists())
        state = SyncState.objects.using(NODE).get(clinic_tin=TIN)
        self.assertIsNotNone(state.seeded_at)

    def test_seed_completes_an_approved_switch_to_offline(self):
        TenantOpsModeChangeRequest.objects.create(
            clinic_tin=TIN, current_ops_mode="online", requested_ops_mode="offline",
            status="approved", reviewed_at=timezone.now(),
        )
        self.sync("sync")  # a plain sync does not finish a seed
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "online")
        self.seed_node()
        self.tenant.refresh_from_db()
        self.assertEqual(self.tenant.ops_mode, "offline")
        self.assertEqual(TenantAccount.objects.using(NODE).get(clinic_tin=TIN).ops_mode, "offline")


class PushTests(SyncCase):
    def test_offline_work_reaches_the_cloud_and_stock_moves_by_delta(self):
        med = _med(CLOUD)
        self.seed_node()

        patient = _patient(NODE, mrn="N1")
        enc = _encounter(NODE, patient, number="EN1")
        PaymentTransaction.objects.using(NODE).create(
            encounter=enc, receipt_number="R1", amount=50, tender_method="cash"
        )
        node_med = Medicine.objects.using(NODE).get(sync_uuid=med.sync_uuid)
        node_med.on_hand = 9  # one pack damaged offline
        node_med.save(using=NODE)
        ledger.record_movement(node_med, "damage", "damage:1")
        MedicineDamage.objects.using(NODE).create(
            clinic_tin=TIN, branch_name=BRANCH, medicine=node_med, quantity=1, sale_unit="pack"
        )

        run = self.sync()
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)

        self.assertEqual(Patient.objects.get(mrn="N1").sync_uuid, patient.sync_uuid)
        self.assertEqual(Encounter.objects.get(number="EN1").patient.mrn, "N1")
        self.assertEqual(PaymentTransaction.objects.get(receipt_number="R1").amount, 50)
        self.assertEqual(MedicineDamage.objects.count(), 1)
        self.assertEqual(self.stock(CLOUD, med.sync_uuid), 90)

        again = self.sync()  # nothing new: a second sync changes nothing
        self.assertEqual(again.pull_status, "ok")
        self.assertEqual(self.stock(CLOUD, med.sync_uuid), 90)
        self.assertEqual(PaymentTransaction.objects.count(), 1)
        self.assertEqual(StockMovement.objects.filter(kind="damage").count(), 1)

    def test_a_repeated_push_id_is_applied_once(self):
        med = _med(CLOUD)
        self.seed_node()
        node_med = Medicine.objects.using(NODE).get(sync_uuid=med.sync_uuid)
        node_med.on_hand = 8
        node_med.save(using=NODE)
        ledger.record_movement(node_med, "dispense", "order:1")
        body = {
            "push_id": "11111111-1111-1111-1111-111111111111",
            "entities": export_scope(SCOPE, db=NODE),
        }
        first = self.transport.push(body)
        second = self.transport.push(body)
        self.assertEqual(first, second)
        self.assertEqual(self.stock(CLOUD, med.sync_uuid), 80)

    def test_stock_sold_on_both_sides_adds_up(self):
        med = _med(CLOUD)
        self.seed_node()

        node_med = Medicine.objects.using(NODE).get(sync_uuid=med.sync_uuid)
        node_med.on_hand = 7  # 3 packs dispensed at the clinic
        node_med.save(using=NODE)
        ledger.record_movement(node_med, "dispense", "order:n")

        cloud_med = Medicine.objects.get(pk=med.pk)
        cloud_med.on_hand = 8  # 2 packs dispensed on the cloud meanwhile
        cloud_med.save()
        ledger.record_movement(cloud_med, "dispense", "order:c")

        self.sync()
        self.assertEqual(self.stock(CLOUD, med.sync_uuid), 50)
        self.assertEqual(self.stock(NODE, med.sync_uuid), 50)

    def test_status_only_moves_forward(self):
        patient = _patient(CLOUD)
        enc = _encounter(CLOUD, patient, status="open")
        self.seed_node()
        node_enc = Encounter.objects.using(NODE).get(sync_uuid=enc.sync_uuid)
        node_enc.status = "active"
        node_enc.save(using=NODE)
        cloud_enc = Encounter.objects.get(pk=enc.pk)
        cloud_enc.status = "closed"
        cloud_enc.save()
        self.sync()
        enc.refresh_from_db()
        self.assertEqual(enc.status, "closed")
        self.assertEqual(Encounter.objects.using(NODE).get(sync_uuid=enc.sync_uuid).status, "closed")

    def test_both_sides_editing_a_patient_keeps_the_cloud_copy_and_logs_a_conflict(self):
        patient = _patient(CLOUD, phone="0911")
        self.seed_node()
        node_patient = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        node_patient.phone = "0922"
        node_patient.save(using=NODE)
        Patient.objects.filter(pk=patient.pk).update(phone="0933", updated_at=timezone.now())
        self.sync()
        patient.refresh_from_db()
        self.assertEqual(patient.phone, "0933")
        self.assertEqual(SyncConflict.objects.filter(entity="patients").count(), 1)

    def test_a_node_only_edit_is_applied_on_the_cloud(self):
        patient = _patient(CLOUD, phone="0911")
        self.seed_node()
        node_patient = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        node_patient.phone = "0922"
        node_patient.save(using=NODE)
        self.sync()
        patient.refresh_from_db()
        self.assertEqual(patient.phone, "0922")
        self.assertEqual(SyncConflict.objects.count(), 0)

    def test_the_same_patient_made_on_both_sides_is_merged_not_duplicated(self):
        cloud_patient = _patient(CLOUD, mrn="7")
        node_patient = _patient(NODE, mrn="7")
        run = self.sync()
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"), run.error_message)
        self.assertEqual(Patient.objects.filter(mrn="7").count(), 1)
        node_patient.refresh_from_db(using=NODE)
        self.assertEqual(node_patient.sync_uuid, cloud_patient.sync_uuid)


class SafetyTests(SyncCase):
    def test_a_failed_push_blocks_the_pull_and_a_retry_resumes(self):
        _patient(CLOUD)
        self.seed_node()
        _patient(NODE, mrn="N9")

        class Flaky(LocalTransport):
            fail = True

            def push(self, body):
                if self.fail:
                    raise TransportError("The cloud is unreachable.")
                return super().push(body)

        flaky = Flaky(self.cloud_node)
        run = run_sync(SCOPE, flaky, db=NODE)
        self.assertEqual((run.push_status, run.pull_status), ("failed", "idle"))
        self.assertIn("unreachable", run.error_message)
        self.assertFalse(Patient.objects.filter(mrn="N9").exists())

        flaky.fail = False
        run = run_sync(SCOPE, flaky, run=run, db=NODE)
        self.assertEqual((run.push_status, run.pull_status), ("ok", "ok"))
        self.assertTrue(Patient.objects.filter(mrn="N9").exists())

    def test_a_failed_pull_leaves_the_sync_incomplete(self):
        self.seed_node()
        before = SyncState.objects.using(NODE).get(clinic_tin=TIN).last_successful_sync_at

        class NoPull(LocalTransport):
            def pull(self, body):
                raise TransportError("pull failed")

        run = run_sync(SCOPE, NoPull(self.cloud_node), db=NODE)
        self.assertEqual((run.push_status, run.pull_status), ("ok", "failed"))
        after = SyncState.objects.using(NODE).get(clinic_tin=TIN).last_successful_sync_at
        self.assertEqual(before, after)

    def test_a_row_pointing_outside_the_branch_is_rejected_and_nothing_is_kept(self):
        other_patient = Patient.objects.create(clinic_tin=OTHER, branch_name=BRANCH, mrn="X", full_name="Other")
        _patient(CLOUD, mrn="ok-before")
        entities = {
            "patients": [
                {"sync_uuid": "22222222-2222-2222-2222-222222222222", "stamp": None,
                 "data": {"mrn": "NEW", "full_name": "New"}, "refs": {}, "users": {}}
            ],
            "encounters": [
                {"sync_uuid": "33333333-3333-3333-3333-333333333333", "stamp": None,
                 "data": {"number": "EX", "arrival_type": "new", "status": "open"},
                 "refs": {"patient": str(other_patient.sync_uuid)}, "users": {}}
            ],
        }
        with self.assertRaises(SyncError):
            apply_scope(SCOPE, entities, PUSH, db=CLOUD)
        self.assertFalse(Patient.objects.filter(mrn="NEW").exists())  # rolled back with the bad row

    def test_the_payload_cannot_choose_the_clinic(self):
        entities = {
            "patients": [
                {"sync_uuid": "44444444-4444-4444-4444-444444444444", "stamp": None,
                 "data": {"mrn": "SNEAK", "full_name": "S", "clinic_tin": OTHER, "branch_name": "Elsewhere"},
                 "refs": {}, "users": {}}
            ]
        }
        apply_scope(SCOPE, entities, PUSH, db=CLOUD)
        row = Patient.objects.get(mrn="SNEAK")
        self.assertEqual((row.clinic_tin, row.branch_name), (TIN, BRANCH))

    def test_rows_changed_locally_during_a_pull_are_not_overwritten(self):
        patient = _patient(CLOUD, phone="0911")
        self.seed_node()
        local = Patient.objects.using(NODE).get(sync_uuid=patient.sync_uuid)
        local.phone = "0999"
        local.save(using=NODE)
        entities = export_scope(SCOPE, db=CLOUD)
        entities["patients"][0]["data"]["phone"] = "0000"
        result = apply_scope(
            SCOPE, entities, PULL, db=NODE, local_cutoff=timezone.now() - timedelta(minutes=1)
        )
        local.refresh_from_db(using=NODE)
        self.assertEqual(local.phone, "0999")
        self.assertEqual(result.conflicts, 1)


class HttpEndpointTests(SyncCase):
    def client_with(self, token):
        client = APIClient()
        if token:
            client.credentials(HTTP_AUTHORIZATION=f"Node {token}")
        return client

    def test_push_pull_complete_over_http(self):
        _patient(CLOUD)
        client = self.client_with(self.token)
        pulled = client.post("/api/sync/pull/", {"kind": "seed"}, format="json")
        self.assertEqual(pulled.status_code, 200)
        self.assertEqual(len(pulled.json()["entities"]["patients"]), 1)
        pushed = client.post(
            "/api/sync/push/", {"push_id": "55555555-5555-5555-5555-555555555555", "entities": {}}, format="json"
        )
        self.assertEqual(pushed.status_code, 200)
        done = client.post("/api/sync/complete/", {"kind": "sync", "token": "t"}, format="json")
        self.assertEqual(done.status_code, 200)
        self.assertTrue(SyncState.objects.filter(clinic_tin=TIN).exists())

    def test_a_bad_or_missing_token_is_refused(self):
        self.assertEqual(self.client_with("nope").post("/api/sync/pull/", {}, format="json").status_code, 401)
        self.assertIn(self.client_with(None).post("/api/sync/pull/", {}, format="json").status_code, (401, 403))

    def test_a_staff_login_cannot_use_the_node_endpoints(self):
        user = get_user_model().objects.create_user("mgr", password="x")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.post("/api/sync/pull/", {}, format="json").status_code, 403)

    def test_a_revoked_token_stops_working(self):
        self.cloud_node.is_active = False
        self.cloud_node.save()
        self.assertEqual(self.client_with(self.token).post("/api/sync/pull/", {}, format="json").status_code, 401)

    def test_manager_sees_status_and_cannot_run_on_the_cloud(self):
        user = get_user_model().objects.create_user("mgr2", password="x")
        UserProfile.objects.create(user=user, clinic_tin=TIN, role="manager", branch_name=BRANCH)
        client = APIClient()
        client.force_authenticate(user)
        status = client.get("/api/sync/status/")
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["can_sync"])
        self.assertEqual(client.post("/api/sync/run/", {}, format="json").status_code, 400)
        nurse = get_user_model().objects.create_user("nurse1", password="x")
        UserProfile.objects.create(user=nurse, clinic_tin=TIN, role="nurse", branch_name=BRANCH)
        client.force_authenticate(nurse)
        self.assertEqual(client.get("/api/sync/status/").status_code, 403)
